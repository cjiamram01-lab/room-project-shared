"""
count_api.py

HTTP service around the CCTV person counter:

    POST /count                 count people in an uploaded picture
    POST /count/annotated       same, but returns the picture with boxes drawn
    GET  /stream/{ip_address}   live MJPEG stream with boxes drawn
    GET  /view/{ip_address}     a page that displays that stream
    GET  /classes               the classes YOLO can be asked for
    GET  /health                model, device and stream state

Every endpoint takes an optional `classes` parameter, so the same service can
count people, or laptops, or both, without a redeploy:

    POST /count?classes=person
    POST /count?classes=person,laptop,chair
    POST /count?classes=0,63
    POST /count?classes=all

Run it separately from control-api, not inside it: inference holds a worker for
hundreds of milliseconds at a time, and ultralytics drags in ~3 GB of torch
that the booking API has no use for.

    uvicorn count_api:app --host 0.0.0.0 --port 8010
    python count_api.py --device cuda --port 8010

Serve it over HTTPS when the page embedding the stream is itself on https://,
or the browser blocks the <img> as mixed content:

    python count_api.py --ssl                       # the project's Apache pair
    python count_api.py --ssl-keyfile key.pem --ssl-certfile cert.pem
"""

from __future__ import annotations

import argparse
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import uuid4
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse

from cctv_common import (
    DetectionParams,
    build_rtsp_url,
    detect,
    draw_annotations,
    load_config,
    load_model,
    mask_url,
    resolve_classes,
    resolve_device,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("count-api")

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_CONCURRENT_STREAMS = 4
JPEG_QUALITY = 80
# A stream nobody closed is dropped after this long.
#
# This is the backstop that actually matters. A client that dies without
# closing its TCP connection (a killed tab, a sleeping laptop, a proxy holding
# the socket) leaves the generator blocked on a write that never fails, so it
# never reaches its stop check and never releases. Measured: a SIGKILLed
# client holds its slot indefinitely. Ten minutes suits a "look at the camera"
# session and bounds the damage; raise it with --max-stream-seconds for a wall
# display that needs to stay up.
MAX_STREAM_SECONDS = 600
# How long after that to wait for the generator to notice before the slot is
# taken back by force.
STREAM_GRACE_SECONDS = 15

# The pair Apache already serves cosai.nrru.ac.th with, so --ssl needs no
# arguments on the server box.
DEFAULT_SSL_KEYFILE = "/etc/nginx/ssl/apache.key"
DEFAULT_SSL_CERTFILE = "/etc/nginx/ssl/fullchain.pem"


@dataclass
class StreamHandle:
    """One live stream's slot, and the flag used to ask it to stop."""

    id: str
    ip_address: str
    kind: str
    started_at: float
    stop: threading.Event = field(default_factory=threading.Event)

    @property
    def age(self) -> float:
        return time.monotonic() - self.started_at

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "ip_address": self.ip_address,
            "kind": self.kind,
            "age_seconds": round(self.age, 1),
            "stopping": self.stop.is_set(),
        }


class StreamRegistry:
    """
    The single owner of the concurrent-stream budget.

    A semaphore plus a separate counter can drift apart: any path that released
    one without the other left the limit permanently wrong, invisible and
    unfixable short of a restart. One locked dict is both the budget and the
    record of who holds it, so the two cannot disagree.
    """

    def __init__(self, limit: int, max_seconds: float = MAX_STREAM_SECONDS):
        self._limit = limit
        self._max_seconds = max_seconds
        self._lock = threading.Lock()
        self._handles: dict[str, StreamHandle] = {}

    def acquire(self, ip_address: str, kind: str) -> StreamHandle | None:
        """Take a slot, or None when the budget is spent."""
        with self._lock:
            self._reap()
            if len(self._handles) >= self._limit:
                return None
            handle = StreamHandle(uuid4().hex[:8], ip_address, kind, time.monotonic())
            self._handles[handle.id] = handle
            return handle

    def release(self, handle: StreamHandle) -> None:
        """
        Give a slot back. Idempotent on purpose.

        A double release used to be able to push the budget above the limit;
        popping by id can only ever remove the one slot that was taken.
        """
        with self._lock:
            self._handles.pop(handle.id, None)

    def close(self, stream_id: str) -> bool:
        """Ask one stream to stop; its generator releases the slot on the way out."""
        with self._lock:
            handle = self._handles.get(stream_id)
            if handle is None:
                return False
            handle.stop.set()
            return True

    def close_all(self) -> int:
        """Ask every stream to stop, and report how many were asked."""
        with self._lock:
            for handle in self._handles.values():
                handle.stop.set()
            return len(self._handles)

    def snapshot(self) -> dict:
        with self._lock:
            self._reap()
            return {
                "active": len(self._handles),
                "limit": self._limit,
                "streams": [h.as_dict() for h in self._handles.values()],
            }

    def _reap(self) -> None:
        """
        Expire streams that have outlived the cap. Caller holds the lock.

        Asking first and forcing later: a generator that is merely slow gets a
        grace period to exit and release itself, so the count does not briefly
        exceed the limit. Only one that ignores the flag is cut loose.
        """
        for handle in list(self._handles.values()):
            if handle.age <= self._max_seconds:
                continue
            if not handle.stop.is_set():
                logger.warning(
                    "Stream %s on %s exceeded %ds, asking it to stop",
                    handle.id, handle.ip_address, self._max_seconds,
                )
                handle.stop.set()
            elif handle.age > self._max_seconds + STREAM_GRACE_SECONDS:
                logger.error(
                    "Stream %s on %s did not stop, reclaiming its slot",
                    handle.id, handle.ip_address,
                )
                self._handles.pop(handle.id, None)


@dataclass
class Runtime:
    """Everything the handlers share, built once at startup."""

    model: object = None
    params: DetectionParams = field(default_factory=DetectionParams)
    config: dict = field(default_factory=dict)
    weights: str = "yolov8n.pt"
    # One model served from a threadpool: ultralytics is not documented as
    # thread-safe, and two predicts racing on one CUDA context is not a bug
    # anyone wants to debug from a stack trace.
    lock: threading.Lock = field(default_factory=threading.Lock)
    streams: StreamRegistry = field(default_factory=lambda: StreamRegistry(MAX_CONCURRENT_STREAMS))


runtime = Runtime()
settings = argparse.Namespace(weights="yolov8n.pt", device="auto", imgsz=640, half=None)


@asynccontextmanager
async def lifespan(_: FastAPI):
    device = resolve_device(settings.device)
    half = device.startswith("cuda") if settings.half is None else settings.half
    runtime.config = load_config()
    runtime.weights = settings.weights
    runtime.model = load_model(settings.weights, device)
    runtime.params = DetectionParams(imgsz=settings.imgsz, device=device, half=half)
    logger.info("Loaded %s on %s (half=%s)", settings.weights, device, half)
    yield


# Path prefix this API is mounted under by the reverse proxy. Apache strips it
# before forwarding, so routes stay declared without it; root_path only tells
# FastAPI how to build the docs/openapi links the browser has to call back.
# Set ROOT_PATH= (empty) to serve the API from the host root again.
ROOT_PATH = os.getenv("ROOT_PATH", "/counter-api").rstrip("/")

app = FastAPI(
    title="CCTV Counter API",
    description="Count and classify what a camera sees, from a picture or a live stream.",
    lifespan=lifespan,
    root_path=ROOT_PATH,
)


def params_for(
    classes: str | None,
    confidence: float | None,
    iou: float | None,
    imgsz: int | None,
) -> DetectionParams:
    """Per-request detection settings layered over the startup defaults."""
    try:
        selected = resolve_classes(runtime.model, classes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return runtime.params.with_overrides(
        confidence=confidence, iou=iou, imgsz=imgsz,
    ).with_classes(selected)


def run_detection(frame, params: DetectionParams):
    """Serialise access to the one model instance."""
    with runtime.lock:
        return detect(frame, runtime.model, params)


def decode_upload(payload: bytes):
    """Bytes to a BGR frame, with a clear 400 when it is not a picture."""
    if not payload:
        raise HTTPException(status_code=400, detail="Empty upload")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Image larger than {MAX_UPLOAD_BYTES // (1024 * 1024)}MB",
        )

    frame = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(status_code=400, detail="Could not decode image (unsupported or corrupt)")
    return frame


def summarise(detections) -> dict:
    """Totals per class, so a caller asking for several gets a usable breakdown."""
    counts: dict[str, int] = {}
    for detection in detections:
        counts[detection.label] = counts.get(detection.label, 0) + 1
    return counts


# These handlers are sync on purpose: FastAPI runs a `def` endpoint in its
# threadpool, so a 300ms inference never blocks the event loop the way the
# same work inside an `async def` would.
@app.post("/count")
def count_picture(
    file: UploadFile = File(..., description="Image to count"),
    classes: str | None = Query(None, description="Names or ids, comma separated; 'all' for every class"),
    confidence: float | None = Query(None, ge=0.0, le=1.0),
    iou: float | None = Query(None, ge=0.0, le=1.0),
    imgsz: int | None = Query(None, ge=32, le=4096),
):
    """Count what YOLO finds in an uploaded picture."""
    started = time.perf_counter()
    frame = decode_upload(file.file.read())
    params = params_for(classes, confidence, iou, imgsz)
    detections = run_detection(frame, params)

    height, width = frame.shape[:2]
    return {
        "count": len(detections),
        "counts_by_class": summarise(detections),
        "detections": [d.as_dict() for d in detections],
        "image": {"width": width, "height": height},
        "model": {
            "weights": runtime.weights,
            "device": params.device,
            "imgsz": params.imgsz,
            "confidence": params.confidence,
            "classes": list(params.classes) if params.classes else "all",
        },
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


@app.post("/count/annotated", response_class=Response, responses={200: {"content": {"image/jpeg": {}}}})
def count_picture_annotated(
    file: UploadFile = File(..., description="Image to annotate"),
    classes: str | None = Query(None),
    confidence: float | None = Query(None, ge=0.0, le=1.0),
    iou: float | None = Query(None, ge=0.0, le=1.0),
    imgsz: int | None = Query(None, ge=32, le=4096),
):
    """The same count, returned as a JPEG with the boxes drawn on it."""
    frame = decode_upload(file.file.read())
    params = params_for(classes, confidence, iou, imgsz)
    detections = run_detection(frame, params)

    annotated = draw_annotations(frame, detections, f"Count: {len(detections)}")
    ok, buffer = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        raise HTTPException(status_code=500, detail="Could not encode the annotated image")

    # The count goes in a header too, so a caller showing the image does not
    # have to send the picture a second time to /count to learn the number.
    return Response(
        content=buffer.tobytes(),
        media_type="image/jpeg",
        headers={"X-Detection-Count": str(len(detections))},
    )


def open_camera(ip_address: str) -> cv2.VideoCapture:
    """
    Connect to a camera, raising the right HTTP error if it will not open.

    Deliberately not inside the generator: a generator body does not run until
    the first frame is pulled, by which point the response has already gone out
    as a 200 multipart and an HTTPException can no longer set a status code.
    """
    url = build_rtsp_url(ip_address, runtime.config)
    if url is None:
        raise HTTPException(status_code=500, detail="No camera credentials configured")

    capture = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not capture.isOpened():
        capture.release()
        logger.warning("Stream requested for unreachable camera %s", mask_url(url))
        raise HTTPException(status_code=502, detail=f"Could not connect to camera {ip_address}")

    capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return capture


def mjpeg_frames(
    capture: cv2.VideoCapture,
    handle: StreamHandle,
    params: DetectionParams | None,
    fps: float,
):
    """
    Yield JPEG parts until the client goes away.

    params is None for a plain camera view: no inference runs at all, which is
    both what someone checking a camera by eye wants to see and far cheaper —
    watching a feed should not occupy the model the counters depend on.

    Takes an already-open capture and owns it from here: the finally below is
    the one place the camera and the stream slot are released, and Starlette
    closes the generator even when the client disconnects on the first frame.
    """
    ip_address = handle.ip_address
    logger.info("Stream %s open for %s (%s)", handle.id, ip_address, handle.kind)

    interval = 1.0 / fps if fps > 0 else 0.0
    try:
        while not handle.stop.is_set():
            cycle_started = time.monotonic()
            ok, frame = capture.read()
            if not ok:
                logger.warning("Stream for %s dropped", ip_address)
                break

            if params is not None:
                detections = run_detection(frame, params)
                frame = draw_annotations(frame, detections, f"Count: {len(detections)}")

            encoded, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if not encoded:
                continue

            yield (
                b"--frame\r\nContent-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(buffer)).encode() + b"\r\n\r\n"
                + buffer.tobytes() + b"\r\n"
            )

            # Throttle to the requested rate. Without this the loop runs every
            # camera frame through YOLO and one viewer saturates the GPU.
            remaining = interval - (time.monotonic() - cycle_started)
            if remaining > 0:
                # Waiting on the stop flag rather than sleeping blind, so a
                # close request is acted on immediately instead of after the
                # current frame interval.
                handle.stop.wait(remaining)
    finally:
        # The one disposal path: it runs on a client disconnect, on an error,
        # on a stop request and on a normal end, because Starlette closes the
        # generator in every one of those cases.
        capture.release()
        runtime.streams.release(handle)
        logger.info("Stream %s closed for %s", handle.id, ip_address)


@app.get("/stream/{ip_address}")
def stream_camera(
    ip_address: str,
    classes: str | None = Query(None),
    confidence: float | None = Query(None, ge=0.0, le=1.0),
    iou: float | None = Query(None, ge=0.0, le=1.0),
    imgsz: int | None = Query(None, ge=32, le=4096),
    fps: float = Query(5.0, gt=0.0, le=30.0, description="Annotated frames per second"),
):
    """Live MJPEG from one camera, with detections drawn on each frame."""
    params = params_for(classes, confidence, iou, imgsz)

    # Taken before the camera is opened, and handed to the generator's finally
    # only once the connection succeeded — otherwise a run of unreachable
    # cameras would use up every slot and lock out the working ones.
    handle = runtime.streams.acquire(ip_address, "annotated")
    if handle is None:
        raise HTTPException(
            status_code=429,
            detail=f"Too many live streams open (limit {MAX_CONCURRENT_STREAMS})",
        )

    try:
        capture = open_camera(ip_address)
    except Exception:
        # The slot was taken before the camera was opened, so a camera that
        # refuses must hand it straight back or failures would eat the budget.
        runtime.streams.release(handle)
        raise

    return StreamingResponse(
        mjpeg_frames(capture, handle, params, fps),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/raw/{ip_address}")
def stream_camera_raw(
    ip_address: str,
    fps: float = Query(10.0, gt=0.0, le=30.0, description="Frames per second to relay"),
):
    """
    Live MJPEG from one camera, straight through — no detection, no boxes.

    Defaults to a higher frame rate than /stream because nothing here waits on
    the model; the only cost is decode and re-encode.
    """
    handle = runtime.streams.acquire(ip_address, "raw")
    if handle is None:
        raise HTTPException(
            status_code=429,
            detail=f"Too many live streams open (limit {MAX_CONCURRENT_STREAMS})",
        )

    try:
        capture = open_camera(ip_address)
    except Exception:
        # The slot was taken before the camera was opened, so a camera that
        # refuses must hand it straight back or failures would eat the budget.
        runtime.streams.release(handle)
        raise

    return StreamingResponse(
        mjpeg_frames(capture, handle, None, fps),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/view/{ip_address}", response_class=HTMLResponse)
def view_camera(
    request: Request,
    ip_address: str,
    fps: float = Query(10.0, gt=0, le=30),
):
    """
    A page showing the plain camera, for checking it by eye.

    Points at /raw, not /stream: this is for seeing whether the camera is
    aimed and focused, which boxes and a count only get in the way of. Open
    /stream/{ip} directly to watch the detections.
    """
    # The browser resolves this src against the public URL, which still carries
    # the prefix Apache stripped before forwarding — so a bare "/stream/..."
    # would 404 on the proxy. root_path is how FastAPI reports that prefix.
    prefix = request.scope.get("root_path", "").rstrip("/")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Camera {ip_address}</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{ margin: 0; display: grid; place-items: center; min-height: 100vh;
           gap: 1rem; padding: 1rem; background: #0f172a; color: #e2e8f0;
           font: 15px/1.5 system-ui, sans-serif; }}
    h1 {{ margin: 0; font-size: 1rem; font-weight: 600; letter-spacing: .02em; }}
    img {{ max-width: 100%; border-radius: 10px; box-shadow: 0 10px 40px rgb(0 0 0 / .45); }}
    p {{ margin: 0; opacity: .7; font-size: .82rem; }}
  </style>
</head>
<body>
  <h1>Camera {ip_address}</h1>
  <img src="{prefix}/raw/{ip_address}?fps={fps}" alt="Live view of camera {ip_address}" />
  <p>{fps} fps &middot; no detection</p>
</body>
</html>"""


@app.get("/streams")
def list_streams():
    """
    What is currently holding a stream slot, and for how long.

    Without this, a budget stuck at the limit gives no clue whether real
    viewers are watching or a slot has been stranded.
    """
    return runtime.streams.snapshot()


@app.delete("/streams")
def close_streams():
    """Ask every open stream to stop — the manual way out of an exhausted budget."""
    asked = runtime.streams.close_all()
    logger.info("Close requested for %d stream(s)", asked)
    return {"closing": asked, "streams": runtime.streams.snapshot()}


@app.delete("/streams/{stream_id}")
def close_stream(stream_id: str):
    """Ask one stream to stop, by the id /streams reports."""
    if not runtime.streams.close(stream_id):
        raise HTTPException(status_code=404, detail=f"No open stream with id {stream_id}")
    return {"closing": stream_id}


@app.get("/classes")
def list_classes():
    """Every class the loaded weights can detect, for building a `classes` value."""
    return {
        "weights": runtime.weights,
        "count": len(runtime.model.names),
        "classes": [{"id": i, "name": n} for i, n in sorted(runtime.model.names.items())],
    }


@app.get("/health")
def health():
    """Enough to tell a loaded, GPU-backed service from a limping one."""
    return {
        "status": "ok",
        "model": {
            "weights": runtime.weights,
            "device": runtime.params.device,
            "half": runtime.params.half,
            "imgsz": runtime.params.imgsz,
        },
        "streams": runtime.streams.snapshot(),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the CCTV counter over HTTP.")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0).")
    parser.add_argument("--port", type=int, default=8010, help="Bind port (default: 8010).")
    parser.add_argument("--model", dest="weights", default="yolov8n.pt", help="YOLO weights (default: yolov8n.pt).")
    parser.add_argument("--device", default="auto", help="auto, cpu, cuda, or a GPU index (default: auto).")
    parser.add_argument("--imgsz", type=int, default=640, help="Default inference resolution (default: 640).")
    parser.add_argument(
        "--max-stream-seconds",
        type=float,
        default=MAX_STREAM_SECONDS,
        help=f"Drop a stream after this long, reclaiming its slot (default: {MAX_STREAM_SECONDS:g}).",
    )
    parser.add_argument(
        "--max-streams",
        type=int,
        default=MAX_CONCURRENT_STREAMS,
        help=f"Concurrent stream limit (default: {MAX_CONCURRENT_STREAMS}).",
    )
    parser.add_argument(
        "--half",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="FP16 inference (default: on for CUDA, off for CPU).",
    )

    tls = parser.add_argument_group("TLS")
    tls.add_argument(
        "--ssl-keyfile",
        default=None,
        help=f"Private key, e.g. {DEFAULT_SSL_KEYFILE}. Serves HTTPS when given with --ssl-certfile.",
    )
    tls.add_argument(
        "--ssl-certfile",
        default=None,
        help=f"Certificate, e.g. {DEFAULT_SSL_CERTFILE}. Use the full chain if the CA is not a well-known root.",
    )
    tls.add_argument("--ssl-keyfile-password", default=None, help="Passphrase, if the key is encrypted.")
    tls.add_argument("--ssl-ca-certs", default=None, help="CA bundle, for verifying client certificates.")
    tls.add_argument(
        "--ssl",
        action="store_true",
        help=f"Shorthand for the project's Apache pair ({DEFAULT_SSL_KEYFILE} and {DEFAULT_SSL_CERTFILE}).",
    )

    args = parser.parse_args(argv)

    if args.ssl:
        args.ssl_keyfile = args.ssl_keyfile or DEFAULT_SSL_KEYFILE
        args.ssl_certfile = args.ssl_certfile or DEFAULT_SSL_CERTFILE

    # Half a TLS config silently serves plain HTTP: uvicorn ignores a keyfile
    # with no certfile. A dashboard on https:// would then block the stream as
    # mixed content and the cause would not be obvious from either end.
    if bool(args.ssl_keyfile) != bool(args.ssl_certfile):
        parser.error("--ssl-keyfile and --ssl-certfile must be given together")

    for label, path in (("key", args.ssl_keyfile), ("certificate", args.ssl_certfile)):
        if path and not Path(path).is_file():
            parser.error(f"TLS {label} not found: {path}")

    return args


if __name__ == "__main__":
    import uvicorn

    args = parse_args()
    settings.weights, settings.device = args.weights, args.device
    settings.imgsz, settings.half = args.imgsz, args.half
    runtime.streams = StreamRegistry(args.max_streams, args.max_stream_seconds)
    logger.info(
        "Stream budget: %d concurrent, dropped after %gs",
        args.max_streams, args.max_stream_seconds,
    )

    scheme = "https" if args.ssl_certfile else "http"
    logger.info("Serving on %s://%s:%d", scheme, args.host, args.port)
    if scheme == "http":
        # Not an error — plenty of deployments sit behind a TLS proxy — but
        # worth stating, because the browser-side failure it causes shows up
        # as a blocked image with no server-side trace.
        logger.info("No TLS: an https:// page will block this stream as mixed content")

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        ssl_keyfile=args.ssl_keyfile,
        ssl_certfile=args.ssl_certfile,
        ssl_keyfile_password=args.ssl_keyfile_password,
        ssl_ca_certs=args.ssl_ca_certs,
    )
