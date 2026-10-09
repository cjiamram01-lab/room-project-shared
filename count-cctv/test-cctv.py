#!/usr/bin/env python3
"""
test-cctv.py

Count people on an RTSP CCTV stream with a YOLO model (Ultralytics), filtered
to the COCO "person" class, and publish each stable count to MQTT using the
same topic and payload the webcam counter sends, so control-api's
camera/person-count subscriber picks this up unchanged.

Usage:
    python test-cctv.py
    python test-cctv.py --url rtsp://user:pass@10.100.197.21:554/
    python test-cctv.py --model yolov8s.pt --confidence 0.4
    python test-cctv.py --imgsz 1280          # small, distant people
    python test-cctv.py --no-display          # headless, Ctrl+C to stop
    python test-cctv.py --no-mqtt             # just watch, publish nothing

The camera URL carries a password, so it is read from CAM_URL or config.json
rather than living in this file:
    set CAM_URL=rtsp://admin:secret@10.100.197.21:554/       (Windows)
    export CAM_URL='rtsp://admin:secret@10.100.197.21:554/'  (Linux)

Exit codes:
    0 - success
    1 - no camera URL configured
    2 - model failed to load
    3 - stream could not be opened
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# Force TCP: more stable than UDP on a busy network. FFmpeg reads this when
# VideoCapture opens, so it only has to be set before the first connection.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import cv2
import paho.mqtt.client as mqtt
from ultralytics import YOLO

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

COCO_PERSON_CLASS_ID = 0
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "config.json"
# The webcam counter already downloaded these; reuse them instead of pulling
# the same weights again into this folder.
SHARED_WEIGHTS_DIR = SCRIPT_DIR.parent / "face-counter"


@dataclass(frozen=True)
class DetectionParams:
    """Parameters controlling YOLO person detection sensitivity."""

    confidence: float = 0.5  # minimum detection confidence to keep a box
    iou: float = 0.45        # IoU threshold used for NMS
    imgsz: int = 640         # inference resolution; raise it for distant people


def load_config(config_path: Path) -> dict:
    """Load config.json, returning {} (and logging) if missing or invalid."""
    if not config_path.is_file():
        logger.warning("Config file not found: %s — proceeding without it", config_path)
        return {}

    try:
        return json.loads(config_path.read_text())
    except json.JSONDecodeError as exc:
        logger.warning("Could not parse config file %s (%s) — proceeding without it", config_path, exc)
        return {}


def resolve_weights(weights: str) -> str:
    """Prefer a copy already sitting next to the webcam counter, if there is one."""
    if Path(weights).is_file():
        return weights

    shared = SHARED_WEIGHTS_DIR / weights
    if shared.is_file():
        logger.info("Using existing weights at %s", shared)
        return str(shared)

    return weights  # let ultralytics download it


def load_model(weights: str) -> YOLO:
    """Load a YOLO model, downloading pretrained weights on first use."""
    try:
        return YOLO(resolve_weights(weights))
    except Exception as exc:  # ultralytics raises plain Exception on bad weights
        raise RuntimeError(f"Failed to load YOLO model '{weights}': {exc}") from exc


def count_persons(frame, model: YOLO, params: DetectionParams) -> list[tuple[int, int, int, int]]:
    """
    Detect people in a frame.

    Returns a list of bounding boxes as (x, y, width, height) tuples.
    """
    results = model.predict(
        frame,
        classes=[COCO_PERSON_CLASS_ID],
        conf=params.confidence,
        iou=params.iou,
        imgsz=params.imgsz,
        verbose=False,
    )

    boxes: list[tuple[int, int, int, int]] = []
    for box in results[0].boxes:
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        boxes.append((int(x1), int(y1), int(x2 - x1), int(y2 - y1)))
    return boxes


def draw_annotations(frame, persons: list[tuple[int, int, int, int]], label: str):
    """Return a copy of the frame with people boxed and a count drawn on top."""
    annotated = frame.copy()
    for (x, y, w, h) in persons:
        cv2.rectangle(annotated, (x, y), (x + w, y + h), (0, 255, 0), 2)

    cv2.putText(
        annotated, label, (10, 30),
        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2, cv2.LINE_AA,
    )
    return annotated


class StreamReader:
    """
    Background reader that holds only the newest frame.

    YOLO is slower than the camera's frame rate, so reading on the main thread
    would hand us whatever FFmpeg buffered while the last inference ran, and the
    overlay would fall further behind real time the longer the script stayed up.
    Dropping stale frames keeps the count current at the cost of frames we were
    never going to look at.
    """

    RECONNECT_DELAYS = (1, 2, 5, 10, 15)

    def __init__(self, url: str):
        self._url = url
        self._capture: cv2.VideoCapture | None = None
        self._frame = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.alive = False

    def open(self) -> bool:
        self._capture = self._connect()
        if self._capture is None:
            return False

        self.alive = True
        self._thread = threading.Thread(target=self._pump, name="rtsp-reader", daemon=True)
        self._thread.start()
        return True

    def _connect(self) -> cv2.VideoCapture | None:
        capture = cv2.VideoCapture(self._url, cv2.CAP_FFMPEG)
        if not capture.isOpened():
            capture.release()
            return None
        # Ask the backend itself not to queue frames. Honoured by some FFmpeg
        # builds and ignored by others, which is why the thread drops frames too.
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return capture

    def _pump(self) -> None:
        while not self._stop.is_set():
            ok, frame = self._capture.read()
            if not ok:
                logger.warning("Frame grab failed, stream dropped")
                if not self._reconnect():
                    break
                continue

            with self._lock:
                self._frame = frame

        self.alive = False

    def _reconnect(self) -> bool:
        self._capture.release()
        for delay in self.RECONNECT_DELAYS:
            if self._stop.wait(delay):
                return False

            logger.info("Reconnecting to the stream...")
            capture = self._connect()
            if capture is not None:
                self._capture = capture
                logger.info("Reconnected")
                return True

        logger.error("Could not reconnect to the stream, giving up")
        return False

    def read(self):
        """Return the newest unseen frame, or None if none arrived since the last call."""
        with self._lock:
            frame, self._frame = self._frame, None
        return frame

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._capture is not None:
            self._capture.release()


class CountStabiliser:
    """
    Hold a changed count until it repeats, so one bad frame cannot publish.

    On a wide CCTV shot the raw count flickers as people overlap, turn away or
    walk behind a monitor. Publishing every raw change would spam the broker and
    make the dashboard twitch, so a count has to survive a few frames to count.
    """

    def __init__(self, required_frames: int = 3):
        self._required = required_frames
        self._candidate: int | None = None
        self._streak = 0
        self.stable: int | None = None

    def update(self, count: int) -> int | None:
        """Feed one frame's count; returns the new stable count, or None if unchanged."""
        if count == self._candidate:
            self._streak += 1
        else:
            self._candidate = count
            self._streak = 1

        if self._streak >= self._required and count != self.stable:
            self.stable = count
            return count
        return None


def connect_mqtt(host: str, port: int) -> mqtt.Client | None:
    """Connect to the MQTT broker, returning None (and logging) on failure."""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="cctv-person-counter")
    try:
        client.connect(host, port)
        client.loop_start()
        logger.info("Connected to MQTT broker at %s:%d", host, port)
        return client
    except OSError as exc:
        logger.warning(
            "Could not connect to MQTT broker at %s:%d (%s) — continuing without publishing",
            host, port, exc,
        )
        return None


def run_stream(
    url: str,
    model: YOLO,
    params: DetectionParams,
    mqtt_client: mqtt.Client | None = None,
    mqtt_topic: str = "camera/person-count",
    room_no: str | None = None,
    stable_frames: int = 3,
    display: bool = True,
) -> int:
    """
    Read the CCTV stream, count people on each frame, and publish stable counts.

    Publishes {"room_no": room_no, "count": N} to mqtt_topic whenever the count
    settles on a new value. With display on, press 'q' or Esc in the window to
    quit; headless, Ctrl+C stops the loop.
    """
    reader = StreamReader(url)
    if not reader.open():
        logger.error("Cannot open stream: check the URL path, credentials, and network")
        return 3

    stabiliser = CountStabiliser(stable_frames)
    stream_ended = False
    window_name = "CCTV Person Counter (press q to quit)"
    if display:
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(window_name, 1280, 720)

    try:
        while True:
            if not reader.alive:
                logger.error("Stream ended")
                stream_ended = True
                break

            frame = reader.read()
            if frame is None:
                # No new frame yet. Keep the window responsive while we wait.
                if display:
                    if cv2.waitKey(10) & 0xFF in (27, ord("q")):
                        break
                else:
                    time.sleep(0.01)
                continue

            persons = count_persons(frame, model, params)
            settled = stabiliser.update(len(persons))

            if settled is not None and mqtt_client is not None:
                payload = json.dumps({"room_no": room_no, "count": settled})
                mqtt_client.publish(mqtt_topic, payload)
                logger.info("Published room_no=%s count=%d to %s", room_no, settled, mqtt_topic)

            if not display:
                continue

            label = f"People: {len(persons)}"
            if stabiliser.stable is not None and stabiliser.stable != len(persons):
                label += f"  (published: {stabiliser.stable})"

            cv2.imshow(window_name, draw_annotations(frame, persons, label))
            if cv2.waitKey(1) & 0xFF in (27, ord("q")):  # Esc or q to quit
                break
    except KeyboardInterrupt:
        logger.info("Interrupted, stopping.")
    finally:
        reader.close()
        cv2.destroyAllWindows()

    return 3 if stream_ended else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Count people on an RTSP CCTV stream using YOLO."
    )
    parser.add_argument(
        "--url",
        default=None,
        help="RTSP stream URL (default: CAM_URL env var, then 'url' from --config).",
    )
    parser.add_argument(
        "--model",
        default="yolov8n.pt",
        help="Ultralytics YOLO weights to use (default: yolov8n.pt; reused from "
        "../face-counter when present, downloaded otherwise).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config.json containing url, room_no and host (default: {DEFAULT_CONFIG_PATH.name} next to this script).",
    )
    parser.add_argument(
        "--room-no",
        default=None,
        help="Room number/code sent in the MQTT payload (default: room_no from --config).",
    )
    parser.add_argument(
        "--mqtt-host",
        default=None,
        help="MQTT broker host to publish person counts to "
        "(default: host from --config, falling back to 192.168.16.112).",
    )
    parser.add_argument(
        "--mqtt-port",
        type=int,
        default=1883,
        help="MQTT broker port (default: 1883).",
    )
    parser.add_argument(
        "--mqtt-topic",
        default="camera/person-count",
        help="MQTT topic to publish person counts to "
        "(default: camera/person-count; room_no is sent in the JSON payload).",
    )
    parser.add_argument(
        "--mqtt",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Publish counts to MQTT (default: on). Use --no-mqtt to only watch.",
    )
    parser.add_argument(
        "--display",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Show the live video window (default: on). "
        "Use --no-display to run headless (Ctrl+C to stop).",
    )
    parser.add_argument(
        "--confidence",
        type=float,
        default=0.5,
        help="Minimum detection confidence to keep a box (default: 0.5).",
    )
    parser.add_argument(
        "--iou",
        type=float,
        default=0.45,
        help="IoU threshold used for non-max suppression (default: 0.45).",
    )
    parser.add_argument(
        "--imgsz",
        type=int,
        default=640,
        help="Inference resolution (default: 640). Try 1280 when the camera is "
        "far from the desks and small people are being missed.",
    )
    parser.add_argument(
        "--stable-frames",
        type=int,
        default=3,
        help="Consecutive frames a changed count must hold before it is published (default: 3).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)

    url = args.url or os.environ.get("CAM_URL") or config.get("url")
    if not url:
        logger.error(
            'No camera URL. Pass --url, set CAM_URL, or add "url" to %s.', args.config
        )
        return 1

    try:
        model = load_model(args.model)
    except RuntimeError as exc:
        logger.error(exc)
        return 2

    params = DetectionParams(
        confidence=args.confidence,
        iou=args.iou,
        imgsz=args.imgsz,
    )

    room_no = args.room_no or config.get("room_no")
    if room_no:
        logger.info("Using room_no=%s", room_no)
    else:
        logger.warning(
            "No room_no — control-api files person counts by room, so it will ignore these"
        )

    mqtt_client = None
    if args.mqtt:
        mqtt_host = args.mqtt_host or config.get("host") or "192.168.16.112"
        mqtt_client = connect_mqtt(mqtt_host, args.mqtt_port)

    try:
        return run_stream(
            url,
            model,
            params,
            mqtt_client=mqtt_client,
            mqtt_topic=args.mqtt_topic,
            room_no=room_no,
            stable_frames=args.stable_frames,
            display=args.display,
        )
    finally:
        if mqtt_client is not None:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()


if __name__ == "__main__":
    sys.exit(main())
