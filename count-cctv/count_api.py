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
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse

from cctv_common import (
    DetectionParams,
    build_rtsp_url,
    camera_reachable,
    detect,
    detect_tiled,
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
# 0 means no limit, which is the default: turning a viewer away was costing
# more than letting it through. Set a positive --max-streams to cap it again.
MAX_CONCURRENT_STREAMS = 0
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
    # Opaque id for the browser tab watching this stream, so its next request
    # can take over from this one instead of queueing behind it. Empty when
    # the caller did not send one, which never supersedes anything.
    viewer: str = ""

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
            "viewer": self.viewer,
        }


class StreamRegistry:
    """
    The register of open streams, and the budget when there is one.

    A semaphore plus a separate counter can drift apart: any path that released
    one without the other left the limit permanently wrong, invisible and
    unfixable short of a restart. One locked dict is both the budget and the
    record of who holds it, so the two cannot disagree.

    limit <= 0 means no cap. The register is still kept: /streams is how a
    stuck stream is found, and the time cap and the viewer handover both work
    off it whether or not anything is being counted against a limit.
    """

    def __init__(self, limit: int, max_seconds: float = MAX_STREAM_SECONDS):
        self._limit = limit
        self._max_seconds = max_seconds
        self._lock = threading.Lock()
        self._handles: dict[str, StreamHandle] = {}

    def acquire(self, ip_address: str, kind: str, viewer: str = "") -> StreamHandle | None:
        """
        Take a slot, or None when the budget is spent.

        Returns a handle unless a limit is set and already reached.

        A viewer id replaces that viewer's own earlier stream rather than
        leaving both open. Switching between the raw and annotated views, or
        between cameras, opens the new stream before the browser has finished
        closing the old one, and a dropped client is only noticed when the
        next write fails — tens of seconds later. Without this, one person
        toggling twice would leave three streams running for one picture.
        """
        with self._lock:
            self._reap()
            if viewer:
                self._supersede(viewer)
            if 0 < self._limit <= len(self._handles):
                return None
            handle = StreamHandle(uuid4().hex[:8], ip_address, kind, time.monotonic(), viewer=viewer)
            self._handles[handle.id] = handle
            return handle

    @property
    def limit(self) -> int:
        """The configured cap; 0 when there is none."""
        return self._limit

    def _supersede(self, viewer: str) -> None:
        """
        Retire a viewer's earlier streams. Caller holds the lock.

        The slot is handed back here rather than when the old generator
        notices, because the replacement needs it now. The generator still
        runs its own finally, which releases the camera and pops an id that is
        already gone — release is idempotent precisely so this is safe.
        """
        for handle in list(self._handles.values()):
            if handle.viewer != viewer:
                continue
            logger.info(
                "Stream %s on %s superseded by the same viewer",
                handle.id, handle.ip_address,
            )
            handle.stop.set()
            self._handles.pop(handle.id, None)

    def release(self, handle: StreamHandle) -> None:
        """
        Give a slot back. Idempotent on purpose.

        A double release used to be able to push the budget above the limit;
        popping by id can only ever remove the one slot that was taken.
        """
        with self._lock:
            self._handles.pop(handle.id, None)

    def close_viewer(self, viewer: str) -> int:
        """Retire everything one viewer holds, and report how many."""
        if not viewer:
            return 0
        with self._lock:
            held = sum(1 for h in self._handles.values() if h.viewer == viewer)
            self._supersede(viewer)
            return held

    def close(self, stream_id: str) -> bool:
        """
        Stop one stream and drop it from the register straight away.

        The generator still runs its own finally, which releases the camera
        and pops an id that has already gone — release is idempotent so that
        this is safe. Waiting for it instead would leave a stream that has
        been closed still showing as open for as long as it takes the loop to
        reach its next stop check, which on a tiled annotated stream is
        seconds and on a wedged one is never.
        """
        with self._lock:
            handle = self._handles.pop(stream_id, None)
            if handle is None:
                return False
            handle.stop.set()
            return True

    def close_all(self) -> int:
        """Stop every stream and empty the register; reports how many there were."""
        with self._lock:
            handles = list(self._handles.values())
            for handle in handles:
                handle.stop.set()
            self._handles.clear()
            return len(handles)

    def snapshot(self) -> dict:
        with self._lock:
            self._reap()
            return {
                "active": len(self._handles),
                # null rather than 0, so a reader cannot mistake "no limit"
                # for "no room".
                "limit": self._limit if self._limit > 0 else None,
                "streams": [h.as_dict() for h in self._handles.values()],
            }

    def _reap(self) -> None:
        """
        Expire streams that have outlived the cap. Caller holds the lock.

        This is about abandoned work, not about the budget: a stream nobody is
        watching still decodes RTSP and, when annotated, still runs the model.
        Asking first and forcing later gives a generator that is merely slow
        the chance to exit and tidy up after itself; only one that ignores the
        flag is cut loose.
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
                    "Stream %s on %s did not stop, dropping it from the register",
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
    # Stop every open stream on the way down, so a reload does not leave
    # generators holding cameras open against the next process.
    stopped = runtime.streams.close_all()
    if stopped:
        logger.info("Stopped %d stream(s) on shutdown", stopped)


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

# The dashboard lives on another host, so its fetch() calls are cross-origin.
# An <img> stream needs no permission, but reading a JSON count does: without
# this header the browser discards the reply before the page ever sees it.
# Comma-separated list, or "*" for any origin.
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "https://cosai.nrru.ac.th,https://smartlab.nrru.ac.th,https://altosnrru.nrru.ac.th,http://localhost:5173",
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    # The annotated routes report their result in this header, and a browser
    # hides every header but a handful unless they are named here.
    expose_headers=["X-Detection-Count"],
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

    # Checked before FFmpeg is handed the URL: its own timeout on a dead
    # address outlasts the caller's.
    if not camera_reachable(ip_address, runtime.config):
        logger.warning("Camera %s is not answering on its RTSP port", ip_address)
        raise HTTPException(status_code=502, detail=f"Could not connect to camera {ip_address}")

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
    tiles: int = 1,
    overlap: float = 0.2,
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
                # Tiled here for the same reason as in /count: a single pass
                # over a downscaled 1080p frame loses the people at the back of
                # the room, and boxes that disagree with the count on the card
                # are worse than no boxes.
                detections = count_frame(frame, params, tiles, overlap)
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


def grab_still(ip_address: str, warmup: int = 5) -> "cv2.typing.MatLike":
    """
    Take one frame from a camera and hand the slot straight back.

    Uses the same budget as the live streams, because a snapshot opens the
    same RTSP connection; holding it for a second is fine, leaking it is not.
    """
    handle = runtime.streams.acquire(ip_address, "snapshot")
    if handle is None:
        raise HTTPException(
            status_code=429,
            detail=f"Too many live streams open (limit {runtime.streams.limit})",
        )

    try:
        capture = open_camera(ip_address)
    except Exception:
        runtime.streams.release(handle)
        raise

    try:
        # The first frames after an RTSP connect are often partial while the
        # decoder waits for a keyframe, so a few are read and discarded.
        frame = None
        for _ in range(max(warmup, 1)):
            ok, candidate = capture.read()
            if ok:
                frame = candidate
    finally:
        capture.release()
        runtime.streams.release(handle)

    if frame is None:
        raise HTTPException(status_code=502, detail=f"Camera {ip_address} returned no usable frame")
    return frame


def count_frame(frame, params: DetectionParams, tiles: int, overlap: float):
    """Run the detector, tiled or whole-frame, holding the model lock once."""
    with runtime.lock:
        if tiles > 1:
            return detect_tiled(frame, runtime.model, params, tiles, tiles, overlap)
        return detect(frame, runtime.model, params)


@app.get("/count/camera/{ip_address}")
def count_camera(
    ip_address: str,
    tiles: int = Query(3, ge=1, le=6, description="NxN grid of overlapping crops; 1 disables tiling"),
    overlap: float = Query(0.2, ge=0.0, le=0.5, description="How much neighbouring tiles overlap"),
    classes: str | None = Query(None, description="Names or ids, comma separated; 'all' for every class"),
    confidence: float | None = Query(None, ge=0.0, le=1.0),
    iou: float | None = Query(None, ge=0.0, le=1.0),
    imgsz: int | None = Query(None, ge=32, le=4096),
    warmup: int = Query(5, ge=1, le=30, description="Frames to discard while the decoder settles"),
):
    """
    Grab a frame from a camera and count what is in it, tiled by default.

    Tiling matters here more than anywhere: YOLO shrinks a 1920px frame to
    imgsz, which erases anyone a few desks from the camera. Counting the same
    frame again over overlapping crops scales each region up instead, and on
    these rooms it is the difference between finding three people and thirty.
    """
    started = time.perf_counter()
    params = params_for(classes, confidence, iou, imgsz)

    frame = grab_still(ip_address, warmup)
    detections = count_frame(frame, params, tiles, overlap)

    height, width = frame.shape[:2]
    return {
        "ip_address": ip_address,
        "count": len(detections),
        "counts_by_class": summarise(detections),
        "detections": [d.as_dict() for d in detections],
        "image": {"width": width, "height": height},
        "model": {
            "weights": runtime.weights,
            "device": params.device,
            "imgsz": params.imgsz,
            "confidence": params.confidence,
            "tiles": tiles,
            "overlap": overlap,
            "classes": list(params.classes) if params.classes else "all",
        },
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


@app.get(
    "/count/camera/{ip_address}/annotated",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}}},
)
def count_camera_annotated(
    ip_address: str,
    tiles: int = Query(3, ge=1, le=6),
    overlap: float = Query(0.2, ge=0.0, le=0.5),
    classes: str | None = Query(None),
    confidence: float | None = Query(None, ge=0.0, le=1.0),
    iou: float | None = Query(None, ge=0.0, le=1.0),
    imgsz: int | None = Query(None, ge=32, le=4096),
    warmup: int = Query(5, ge=1, le=30),
):
    """The same count, as a JPEG with the boxes drawn — for checking it by eye."""
    params = params_for(classes, confidence, iou, imgsz)
    frame = grab_still(ip_address, warmup)
    detections = count_frame(frame, params, tiles, overlap)

    annotated = draw_annotations(frame, detections, f"Count: {len(detections)}")
    ok, buffer = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        raise HTTPException(status_code=500, detail="Could not encode the annotated image")

    return Response(
        content=buffer.tobytes(),
        media_type="image/jpeg",
        headers={"X-Detection-Count": str(len(detections))},
    )


@app.get("/stream/{ip_address}")
def stream_camera(
    ip_address: str,
    tiles: int = Query(1, ge=1, le=6, description="NxN grid of overlapping crops; 1 disables tiling"),
    overlap: float = Query(0.2, ge=0.0, le=0.5, description="How much neighbouring tiles overlap"),
    classes: str | None = Query(None),
    confidence: float | None = Query(None, ge=0.0, le=1.0),
    iou: float | None = Query(None, ge=0.0, le=1.0),
    imgsz: int | None = Query(None, ge=32, le=4096),
    fps: float = Query(5.0, gt=0.0, le=30.0, description="Annotated frames per second"),
    viewer: str = Query("", max_length=64, description="Caller id; replaces that caller's earlier stream"),
):
    """
    Live MJPEG from one camera, with detections drawn on each frame.

    fps is a ceiling, not a promise: tiles=3 runs nine detections per frame, so
    the real rate is whatever the model manages, and asking for more frames
    than it can produce costs nothing.
    """
    params = params_for(classes, confidence, iou, imgsz)

    # Taken before the camera is opened, and handed to the generator's finally
    # only once the connection succeeded — otherwise a run of unreachable
    # cameras would use up every slot and lock out the working ones.
    handle = runtime.streams.acquire(ip_address, "annotated", viewer)
    if handle is None:
        raise HTTPException(
            status_code=429,
            detail=f"Too many live streams open (limit {runtime.streams.limit})",
        )

    try:
        capture = open_camera(ip_address)
    except Exception:
        # The slot was taken before the camera was opened, so a camera that
        # refuses must hand it straight back or failures would eat the budget.
        runtime.streams.release(handle)
        raise

    return StreamingResponse(
        mjpeg_frames(capture, handle, params, fps, tiles, overlap),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/raw/{ip_address}")
def stream_camera_raw(
    ip_address: str,
    fps: float = Query(10.0, gt=0.0, le=30.0, description="Frames per second to relay"),
    viewer: str = Query("", max_length=64, description="Caller id; replaces that caller's earlier stream"),
):
    """
    Live MJPEG from one camera, straight through — no detection, no boxes.

    Defaults to a higher frame rate than /stream because nothing here waits on
    the model; the only cost is decode and re-encode.
    """
    handle = runtime.streams.acquire(ip_address, "raw", viewer)
    if handle is None:
        raise HTTPException(
            status_code=429,
            detail=f"Too many live streams open (limit {runtime.streams.limit})",
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
    detect_fps: float = Query(2.0, gt=0, le=30, description="Frame ceiling for the annotated view"),
    tiles: int = Query(3, ge=1, le=6, description="Tiling used by the annotated view"),
    overlap: float = Query(0.2, ge=0.0, le=0.5),
):
    """
    A page showing one camera, with or without detection boxes.

    Opens on the plain view, which is what checking a camera's aim and focus
    calls for and costs nothing but a decode. The toggle switches to /stream,
    where every frame goes through the detector.
    """
    # The browser resolves these against the public URL, which still carries
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
    .modes {{ display: flex; gap: .5rem; }}
    .modes button {{ padding: .4rem .9rem; border: 1px solid #334155; border-radius: 999px;
                    background: transparent; color: inherit; font: inherit; font-size: .82rem;
                    cursor: pointer; }}
    .modes button[aria-pressed="true"] {{ border-color: #38bdf8; background: #0b4a6f; color: #e0f2fe; }}
  </style>
</head>
<body>
  <h1>Camera {ip_address}</h1>
  <div class="modes">
    <button type="button" data-mode="raw" aria-pressed="true">Plain</button>
    <button type="button" data-mode="detect" aria-pressed="false">Boxes</button>
  </div>
  <img id="feed" alt="Live view of camera {ip_address}" />
  <p id="note"></p>
<script>
  // One id for this page, so switching mode replaces this viewer's stream on
  // the relay instead of queueing behind it: a dropped MJPEG connection is
  // only noticed when the next write to it fails.
  const viewer = "view-" + Math.random().toString(36).slice(2, 12);
  const prefix = {prefix!r};
  const ip = {ip_address!r};
  const feed = document.getElementById("feed");
  const note = document.getElementById("note");
  const urls = {{
    raw: prefix + "/raw/" + encodeURIComponent(ip) + "?fps={fps}&viewer=" + viewer,
    detect: prefix + "/stream/" + encodeURIComponent(ip)
            + "?fps={detect_fps}&tiles={tiles}&overlap={overlap}&viewer=" + viewer,
  }};
  const notes = {{
    raw: "{fps} fps \u00b7 no detection",
    detect: "up to {detect_fps} fps \u00b7 {tiles}\u00d7{tiles} tiled detection",
  }};
  function show(mode) {{
    for (const button of document.querySelectorAll(".modes button")) {{
      button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
    }}
    // Cache-busted so the browser opens a new connection rather than reviving
    // the closed one.
    feed.src = urls[mode] + "&t=" + Date.now();
    note.textContent = notes[mode];
  }}
  for (const button of document.querySelectorAll(".modes button")) {{
    button.addEventListener("click", () => show(button.dataset.mode));
  }}
  // Hand the slot back on the way out rather than leaving the relay to notice.
  addEventListener("pagehide", () => {{
    fetch(prefix + "/streams/viewer/" + viewer, {{ method: "DELETE", keepalive: true }})
      .catch(() => {{}});
  }});
  show("raw");
</script>
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


@app.delete("/streams/viewer/{viewer}")
def close_viewer_streams(viewer: str):
    """
    Hand back the slots one viewer holds.

    A browser closing an MJPEG connection is only noticed when the next write
    to it fails, which can take a minute. A page that knows it is done — the
    modal closing, the tab unloading — can say so here and free the slot at
    once. Returns 200 even when it held nothing, because the caller is on its
    way out and has nothing useful to do with an error.
    """
    closed = runtime.streams.close_viewer(viewer)
    if closed:
        logger.info("Viewer %s released %d stream(s)", viewer, closed)
    return {"closed": closed}


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
        help="Concurrent stream limit; 0 for no limit (default: 0).",
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
        "Streams: %s concurrent, dropped after %gs",
        args.max_streams if args.max_streams > 0 else "unlimited",
        args.max_stream_seconds,
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
