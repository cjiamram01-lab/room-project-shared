#!/usr/bin/env python3
"""
counter-person.py

Every 5 minutes: ask control-api which rooms are in use right now, count the
people on each of those rooms' cameras with YOLO, publish each count to MQTT
for the live dashboard, and POST the occupied ones to the counter_log endpoint.

Only rooms with a booking covering the current time come back from the usage
endpoint, so cameras in empty rooms are never opened.

Usage:
    python counter-person.py
    python counter-person.py --once                  # one cycle, then exit
    python counter-person.py --interval 600          # every 10 minutes
    python counter-person.py --samples 7 --imgsz 1280
    python counter-person.py --no-mqtt               # log only, publish nothing
    python counter-person.py --dry-run               # count, but write nothing

Camera credentials carry a password, so they come from the environment or
config.json rather than living in this file:
    set CAM_USER=admin & set CAM_PASSWORD=secret      (Windows)
    export CAM_USER=admin CAM_PASSWORD=secret         (Linux)

Exit codes:
    0 - success
    1 - no camera credentials configured
    2 - model failed to load
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

# Force TCP: more stable than UDP on a busy network. FFmpeg reads this when
# VideoCapture opens, so it only has to be set before the first connection.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import cv2
import paho.mqtt.client as mqtt
import requests
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
SHARED_WEIGHTS_DIR = SCRIPT_DIR.parent / "face-counter"

DEFAULT_API_BASE = "http://localhost:8000"
# The route is declared as @router.get("get_current_usage") with no leading
# slash, so FastAPI joins it onto the prefix as one word. Override with
# --usage-path once that slash is added upstream.
DEFAULT_USAGE_PATH = "/room-usageget_current_usage"
DEFAULT_LOG_PATH = "/counter_controller/add/"
DEFAULT_MQTT_HOST = "10.27.127.25"
DEFAULT_MQTT_PORT = 1883
DEFAULT_MQTT_TOPIC = "camera/person-count"


@dataclass(frozen=True)
class DetectionParams:
    """Parameters controlling YOLO person detection sensitivity."""

    confidence: float = 0.5  # minimum detection confidence to keep a box
    iou: float = 0.45        # IoU threshold used for NMS
    imgsz: int = 640         # inference resolution; raise it for distant people


@dataclass(frozen=True)
class Camera:
    """One camera to sample this cycle."""

    ip_address: str
    room_no: str | None


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


def count_persons(frame, model: YOLO, params: DetectionParams) -> int:
    """Number of people YOLO finds in one frame."""
    results = model.predict(
        frame,
        classes=[COCO_PERSON_CLASS_ID],
        conf=params.confidence,
        iou=params.iou,
        imgsz=params.imgsz,
        verbose=False,
    )
    return len(results[0].boxes)


def build_rtsp_url(ip_address: str, config: dict) -> str | None:
    """
    Turn an IP from the usage endpoint into a full RTSP URL.

    The endpoint only knows each room's camera IP; the credentials and stream
    path are the same across the cameras, so they come from config instead of
    being stored per room.
    """
    template = config.get("rtsp_template")
    if template:
        return template.format(ip=ip_address)

    user = os.environ.get("CAM_USER") or config.get("rtsp_user")
    password = os.environ.get("CAM_PASSWORD") or config.get("rtsp_password")
    if not user or not password:
        return None

    port = config.get("rtsp_port", 554)
    path = config.get("rtsp_path", "/")
    if not path.startswith("/"):
        path = "/" + path

    # quote() so a password containing @ / : or ? cannot break the URL apart.
    return f"rtsp://{quote(user, safe='')}:{quote(password, safe='')}@{ip_address}:{port}{path}"


def mask_url(url: str) -> str:
    """Hide the password so a URL can go in a log line."""
    if "@" not in url:
        return url
    creds, _, host = url.rpartition("@")
    scheme, _, userinfo = creds.partition("://")
    user = userinfo.split(":", 1)[0]
    return f"{scheme}://{user}:****@{host}"


def fetch_cameras(session: requests.Session, usage_url: str, timeout: float) -> list[Camera]:
    """
    Cameras for the rooms in use right now, one entry per IP.

    The endpoint UNIONs schedules with room_usages before joining ip_binding, so
    a room sitting in both tables comes back more than once. Deduping by IP
    keeps a camera from being opened and logged twice in the same cycle.
    """
    response = session.get(usage_url, timeout=timeout)
    response.raise_for_status()
    rows = response.json()

    if not isinstance(rows, list):
        raise ValueError(f"Expected a list of usage rows, got {type(rows).__name__}")

    cameras: dict[str, Camera] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ip_address = str(row.get("ip_address") or "").strip()
        if not ip_address:
            logger.warning("Usage row has no ip_address, skipping: %s", row)
            continue
        cameras.setdefault(ip_address, Camera(ip_address, row.get("roomcode")))

    return list(cameras.values())


def sample_count(
    url: str,
    model: YOLO,
    params: DetectionParams,
    samples: int = 5,
    warmup_frames: int = 5,
    open_timeout: float = 15.0,
) -> int | None:
    """
    Open a stream, count people on several frames, and return the median.

    One frame is a poor witness: the frames right after an RTSP connect are
    often partial while the decoder waits for a keyframe, and a person caught
    mid-stride behind a monitor can be missed on any single frame. The median
    discards both kinds of outlier without needing a long observation window.

    Returns None if the stream never produced a usable frame.
    """
    capture = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not capture.isOpened():
        capture.release()
        # Worth separating from "connected but silent": this one points at the
        # credentials, the stream path or the network, not at the camera.
        logger.warning("Could not connect to %s", mask_url(url))
        return None

    capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    deadline = time.monotonic() + open_timeout
    counts: list[int] = []

    try:
        for _ in range(warmup_frames):
            if time.monotonic() > deadline:
                break
            capture.read()  # discard: decoder is still settling

        while len(counts) < samples:
            if time.monotonic() > deadline:
                logger.warning("Timed out after %d/%d samples", len(counts), samples)
                break

            ok, frame = capture.read()
            if not ok:
                # read() normally blocks, but a stream that errors returns
                # immediately — without this the retry loop would spin a core
                # flat out until the timeout.
                time.sleep(0.1)
                continue
            counts.append(count_persons(frame, model, params))
    finally:
        capture.release()

    if not counts:
        return None
    return int(statistics.median(counts))


def post_count(
    session: requests.Session,
    log_url: str,
    ip_address: str,
    count: int,
    timeout: float,
) -> bool:
    """
    POST one count to counter_log.

    The timestamps go on the wire in local time, not UTC: counter_log.log_date
    is a naive DATETIME, and get_current_usage picks its rooms with CURDATE()
    and CURTIME(), so a UTC stamp would file each count hours away from the
    booking slot it belongs to.
    """
    now = datetime.now()
    try:
        response = session.post(
            log_url,
            json={
                "ip_address": ip_address,
                "personal_count": count,
                "log_date": now.isoformat(),
                "log_time": now.strftime("%H:%M:%S"),
                "created_date": now.isoformat(),
            },
            timeout=timeout,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.error("Could not log count for %s: %s", ip_address, exc)
        return False


def connect_mqtt(host: str, port: int) -> mqtt.Client:
    """
    Start an MQTT client that keeps itself connected.

    connect_async rather than connect: this script runs for days, so a broker
    that is down at startup or restarts overnight must not leave it publishing
    into a dead socket for good. paho's network thread retries in the
    background and publishes resume once it is back.
    """
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="cctv-person-counter")
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.connect_async(host, port, keepalive=60)
    client.loop_start()

    # The handshake runs on paho's thread, so give it a moment to land before
    # the first camera is sampled — otherwise a --once run can finish counting
    # and exit while the socket is still being set up.
    deadline = time.monotonic() + 5
    while not client.is_connected() and time.monotonic() < deadline:
        time.sleep(0.1)

    if client.is_connected():
        logger.info("Connected to MQTT broker at %s:%d", host, port)
    else:
        logger.warning(
            "MQTT broker %s:%d not reachable yet — retrying in the background",
            host, port,
        )
    return client


def publish_count(client: mqtt.Client, topic: str, room_no: str | None, count: int) -> bool:
    """
    Publish one count for the live dashboard.

    Payload matches what the webcam counter sends, so control-api's
    camera/person-count subscriber handles both without changes.
    """
    if not client.is_connected():
        logger.warning("MQTT not connected, dropped room=%s count=%d", room_no, count)
        return False

    payload = json.dumps({"room_no": room_no, "count": count})
    info = client.publish(topic, payload, qos=1)
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        logger.warning("MQTT publish failed for room=%s: %s", room_no, mqtt.error_string(info.rc))
        return False
    return True


def run_cycle(
    session: requests.Session,
    model: YOLO,
    params: DetectionParams,
    config: dict,
    args: argparse.Namespace,
    usage_url: str,
    log_url: str,
    mqtt_client: mqtt.Client | None = None,
) -> None:
    """Sample every camera for a room currently in use, then log each count."""
    try:
        cameras = fetch_cameras(session, usage_url, args.api_timeout)
    except (requests.RequestException, ValueError) as exc:
        logger.error("Could not fetch rooms in use from %s: %s", usage_url, exc)
        return

    if not cameras:
        logger.info("No rooms in use right now, nothing to count")
        return

    # Stop before the next cycle is due, so a slow or unreachable camera cannot
    # push every later cycle off its 5-minute slot.
    deadline = time.monotonic() + args.cycle_budget
    logged = failed = skipped = empty = published = 0

    logger.info("%d camera(s) to sample", len(cameras))
    for camera in cameras:
        if time.monotonic() > deadline:
            skipped += 1
            continue

        url = build_rtsp_url(camera.ip_address, config)
        if url is None:
            logger.error("No camera credentials, cannot build a URL for %s", camera.ip_address)
            failed += 1
            continue

        try:
            count = sample_count(
                url, model, params,
                samples=args.samples,
                warmup_frames=args.warmup_frames,
                open_timeout=args.camera_timeout,
            )
        except Exception as exc:
            # One unreachable or misbehaving camera must not end the cycle.
            logger.error("room=%s %s failed: %s", camera.room_no, mask_url(url), exc)
            failed += 1
            continue

        if count is None:
            logger.warning("room=%s %s gave no usable frame", camera.room_no, camera.ip_address)
            failed += 1
            continue

        logger.info("room=%s ip=%s count=%d", camera.room_no, camera.ip_address, count)

        # Published even when the room is empty, unlike the database row below:
        # a live dashboard that never hears "0" would show the last non-zero
        # count until the room was next occupied.
        if mqtt_client is not None:
            if publish_count(mqtt_client, args.mqtt_topic, camera.room_no, count):
                published += 1

        if count <= 0:
            # Only an occupied room earns a row.
            empty += 1
            continue

        if args.dry_run:
            logged += 1
        elif post_count(session, log_url, camera.ip_address, count, args.api_timeout):
            logged += 1
        else:
            failed += 1

    summary = f"Cycle done: {logged} logged, {empty} empty, {failed} failed"
    if mqtt_client is not None:
        summary += f", {published} published"
    if skipped:
        summary += f", {skipped} skipped (ran out of time)"
    logger.info(summary)


def seconds_until_next(interval: int) -> float:
    """
    Seconds until the next interval boundary on the wall clock.

    Sleeping a fixed interval after the work would drift by however long the
    cycle took, so counts would slowly wander off their 5-minute slots.
    """
    return interval - (time.time() % interval)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Count people on the cameras of every room currently in use."
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Seconds between cycles (default: 300, i.e. every 5 minutes).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single cycle and exit, instead of looping.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count and log to the console, but POST nothing to the API.",
    )
    parser.add_argument(
        "--api-base",
        default=None,
        help=f"control-api base URL (default: api_base from --config, falling back to {DEFAULT_API_BASE}).",
    )
    parser.add_argument(
        "--usage-path",
        default=None,
        help=f"Path of the rooms-in-use endpoint (default: {DEFAULT_USAGE_PATH}).",
    )
    parser.add_argument(
        "--log-path",
        default=None,
        help=f"Path of the counter_log add endpoint (default: {DEFAULT_LOG_PATH}).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config.json holding camera credentials and api_base (default: {DEFAULT_CONFIG_PATH.name} next to this script).",
    )
    parser.add_argument(
        "--model",
        default="yolov8n.pt",
        help="Ultralytics YOLO weights to use (default: yolov8n.pt; reused from "
        "../face-counter when present, downloaded otherwise).",
    )
    parser.add_argument(
        "--mqtt",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Publish each count to MQTT for the live dashboard (default: on).",
    )
    parser.add_argument(
        "--mqtt-host",
        default=None,
        help=f"MQTT broker host (default: mqtt_host from --config, falling back to {DEFAULT_MQTT_HOST}).",
    )
    parser.add_argument(
        "--mqtt-port",
        type=int,
        default=None,
        help=f"MQTT broker port (default: mqtt_port from --config, falling back to {DEFAULT_MQTT_PORT}).",
    )
    parser.add_argument(
        "--mqtt-topic",
        default=None,
        help=f"MQTT topic to publish counts to (default: mqtt_topic from --config, "
        f"falling back to {DEFAULT_MQTT_TOPIC}; room_no is sent in the JSON payload).",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=5,
        help="Frames counted per camera, median wins (default: 5).",
    )
    parser.add_argument(
        "--warmup-frames",
        type=int,
        default=5,
        help="Frames discarded after connecting, before counting starts (default: 5).",
    )
    parser.add_argument(
        "--camera-timeout",
        type=float,
        default=15.0,
        help="Seconds to spend on one camera before giving up on it (default: 15).",
    )
    parser.add_argument(
        "--api-timeout",
        type=float,
        default=10.0,
        help="Seconds to wait on an API request (default: 10).",
    )
    parser.add_argument(
        "--cycle-budget",
        type=float,
        default=None,
        help="Seconds a cycle may spend sampling before skipping the rest "
        "(default: 80%% of --interval).",
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
        help="Inference resolution (default: 640). Try 1280 when the cameras are "
        "far from the desks and small people are being missed.",
    )

    args = parser.parse_args(argv)
    if args.cycle_budget is None:
        args.cycle_budget = args.interval * 0.8
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)

    api_base = (args.api_base or config.get("api_base") or DEFAULT_API_BASE).rstrip("/")
    usage_url = api_base + (args.usage_path or config.get("usage_path") or DEFAULT_USAGE_PATH)
    log_url = api_base + (args.log_path or config.get("log_path") or DEFAULT_LOG_PATH)

    # Fail now rather than once per camera on the first cycle.
    if build_rtsp_url("0.0.0.0", config) is None:
        logger.error(
            "No camera credentials. Set CAM_USER and CAM_PASSWORD, or add "
            'rtsp_user/rtsp_password (or rtsp_template) to %s.', args.config
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

    logger.info("Rooms in use: %s", usage_url)
    logger.info("Logging counts to: %s%s", log_url, " (dry run)" if args.dry_run else "")

    # CLI wins over config, config over the built-in default. "host" stays in
    # the chain because test-cctv.py reads the broker from that key.
    mqtt_host = args.mqtt_host or config.get("mqtt_host") or config.get("host") or DEFAULT_MQTT_HOST
    mqtt_port = args.mqtt_port or config.get("mqtt_port") or DEFAULT_MQTT_PORT
    args.mqtt_topic = args.mqtt_topic or config.get("mqtt_topic") or DEFAULT_MQTT_TOPIC

    mqtt_client = None
    if args.mqtt and not args.dry_run:
        mqtt_client = connect_mqtt(mqtt_host, int(mqtt_port))
        logger.info("Publishing counts to: %s", args.mqtt_topic)
    elif args.mqtt:
        logger.info("Dry run, not publishing to MQTT")

    session = requests.Session()
    try:
        while True:
            run_cycle(
                session, model, params, config, args, usage_url, log_url,
                mqtt_client=mqtt_client,
            )

            if args.once:
                break

            wait = seconds_until_next(args.interval)
            logger.info("Next cycle in %.0fs", wait)
            time.sleep(wait)
    except KeyboardInterrupt:
        logger.info("Interrupted, stopping.")
    finally:
        session.close()
        if mqtt_client is not None:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()

    return 0


if __name__ == "__main__":
    sys.exit(main())
