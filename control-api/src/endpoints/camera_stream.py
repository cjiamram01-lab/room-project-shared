"""
Relay a CCTV camera's RTSP feed to the browser as MJPEG.

A browser cannot play RTSP, so each frame is decoded here and re-sent as a
multipart JPEG stream that an <img src="..."> can display directly.

No detection happens here on purpose — counting lives in count-cctv, which
carries torch and ultralytics. This endpoint only moves pictures, so
control-api stays light.
"""

import logging
import os
import time
from urllib.parse import quote

import cv2
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from src.models.ip_binding import IpBinding_Controller

logger = logging.getLogger("camera-stream")

router = APIRouter(
    prefix="/camera-stream",
    tags=["camera-stream"],
    responses={404: {"description": "Not found"}},
)

# Force TCP: more stable than UDP on a busy network.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

MAX_CONCURRENT_STREAMS = 4
JPEG_QUALITY = 75
DEFAULT_FPS = 5.0
# 1080p frames are ~400KB each as JPEG; at 5fps that is 2MB/s per viewer, so
# the relay scales them down unless a caller asks for the full size.
DEFAULT_MAX_WIDTH = 960

_active_streams = 0


def camera_rtsp_url(ip_address: str) -> str:
    """
    Build the RTSP URL for a camera.

    Credentials come from the environment, falling back to config.json, so the
    camera password is not stored per row in ip_binding or committed here.
    """
    template = os.getenv("CAM_RTSP_TEMPLATE")
    if template:
        return template.format(ip=ip_address)

    user = os.getenv("CAM_USER")
    password = os.getenv("CAM_PASSWORD")
    if not user or not password:
        raise HTTPException(
            status_code=500,
            detail="Camera credentials are not configured (set CAM_USER and CAM_PASSWORD)",
        )

    port = os.getenv("CAM_RTSP_PORT", "554")
    path = os.getenv("CAM_RTSP_PATH", "/")
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
    return f"{scheme}://{userinfo.split(':', 1)[0]}:****@{host}"


def known_camera(ip_address: str) -> dict:
    """
    Confirm the IP is a camera registered in ip_binding.

    This endpoint opens a socket to whatever address it is handed, so without
    this check it would happily connect to any host on the internal network on
    behalf of an anonymous caller. Only cameras the system already knows about
    are reachable through it.
    """
    db = IpBinding_Controller()
    try:
        binding = db.get_by_ip(ip_address)
    finally:
        del db

    if binding is None:
        raise HTTPException(status_code=404, detail=f"No camera registered with IP {ip_address}")
    return binding


def open_capture(ip_address: str) -> cv2.VideoCapture:
    """
    Connect to the camera, raising the right HTTP error if it will not open.

    Kept out of the generator below: a generator body does not run until the
    first frame is pulled, by which point the response has already gone out as
    a 200 and an HTTPException can no longer set the status code.
    """
    url = camera_rtsp_url(ip_address)
    capture = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not capture.isOpened():
        capture.release()
        logger.warning("Could not open camera %s", mask_url(url))
        raise HTTPException(status_code=502, detail=f"Could not connect to camera {ip_address}")

    capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return capture


def mjpeg_frames(capture: cv2.VideoCapture, ip_address: str, fps: float, max_width: int):
    """
    Yield JPEG parts until the client goes away.

    Owns the capture from here: the finally below is the one place the camera
    and the stream slot are released, and Starlette closes the generator even
    when the viewer disconnects on the first frame.
    """
    global _active_streams
    _active_streams += 1
    logger.info("Stream open for %s (%d active)", ip_address, _active_streams)

    interval = 1.0 / fps if fps > 0 else 0.0
    try:
        while True:
            started = time.monotonic()
            ok, frame = capture.read()
            if not ok:
                logger.warning("Stream for %s dropped", ip_address)
                break

            if max_width and frame.shape[1] > max_width:
                scale = max_width / frame.shape[1]
                frame = cv2.resize(frame, (max_width, int(frame.shape[0] * scale)))

            encoded, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if not encoded:
                continue

            yield (
                b"--frame\r\nContent-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(buffer)).encode() + b"\r\n\r\n"
                + buffer.tobytes() + b"\r\n"
            )

            # Throttle to the requested rate, so one viewer does not pull every
            # frame the camera produces and saturate the uplink.
            remaining = interval - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
    finally:
        capture.release()
        _active_streams -= 1
        logger.info("Stream closed for %s (%d active)", ip_address, _active_streams)


# Sync handlers on purpose: FastAPI runs a `def` endpoint in its threadpool, so
# decoding never blocks the event loop the booking endpoints share.
@router.get("/cameras")
def list_cameras():
    """Every camera in ip_binding, with the URLs for viewing each one."""
    db = IpBinding_Controller()
    try:
        rows = db.get_all()
    finally:
        del db

    return [
        {
            "room_no": row["room_no"],
            "ip_address": row["ip_address"],
            "stream_url": f"/camera-stream/{row['ip_address']}",
            "view_url": f"/camera-stream/view/{row['ip_address']}",
        }
        for row in rows
    ]


@router.get("/room/{room_no}")
def cameras_for_room(room_no: str):
    """The cameras bound to one room — a room can have more than one."""
    db = IpBinding_Controller()
    try:
        rows = db.get_by_room(room_no)
    finally:
        del db

    if not rows:
        raise HTTPException(status_code=404, detail=f"No camera registered for room {room_no}")

    return [
        {
            "room_no": row["room_no"],
            "ip_address": row["ip_address"],
            "stream_url": f"/camera-stream/{row['ip_address']}",
            "view_url": f"/camera-stream/view/{row['ip_address']}",
        }
        for row in rows
    ]


@router.get("/snapshot/{ip_address}")
def camera_snapshot(ip_address: str, max_width: int = Query(DEFAULT_MAX_WIDTH, ge=160, le=3840)):
    """One JPEG from the camera — cheaper than a stream for a thumbnail."""
    known_camera(ip_address)
    capture = open_capture(ip_address)
    try:
        # The first frames after an RTSP connect are often partial while the
        # decoder waits for a keyframe, so take a few and keep the last.
        frame = None
        for _ in range(5):
            ok, candidate = capture.read()
            if ok:
                frame = candidate
    finally:
        capture.release()

    if frame is None:
        raise HTTPException(status_code=502, detail=f"Camera {ip_address} returned no usable frame")

    if max_width and frame.shape[1] > max_width:
        scale = max_width / frame.shape[1]
        frame = cv2.resize(frame, (max_width, int(frame.shape[0] * scale)))

    encoded, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not encoded:
        raise HTTPException(status_code=500, detail="Could not encode the frame")

    return Response(content=buffer.tobytes(), media_type="image/jpeg")


@router.get("/view/{ip_address}", response_class=HTMLResponse)
def view_camera(ip_address: str, fps: float = Query(DEFAULT_FPS, gt=0, le=30)):
    """A page showing one camera, for checking it by eye."""
    binding = known_camera(ip_address)
    return f"""<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{binding['room_no']} &middot; {ip_address}</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ margin: 0; display: grid; place-items: center; min-height: 100vh; gap: 1rem;
           padding: 1rem; background: #0f172a; color: #e2e8f0;
           font: 15px/1.5 system-ui, sans-serif; }}
    h1 {{ margin: 0; font-size: 1rem; font-weight: 600; }}
    img {{ max-width: 100%; border-radius: 10px; box-shadow: 0 10px 40px rgb(0 0 0 / .45); }}
    p {{ margin: 0; opacity: .7; font-size: .8rem; }}
  </style>
</head>
<body>
  <h1>ห้อง {binding['room_no']} &middot; {ip_address}</h1>
  <img src="/camera-stream/{ip_address}?fps={fps}" alt="ภาพสดจากกล้องห้อง {binding['room_no']}" />
  <p>{fps} fps</p>
</body>
</html>"""


@router.get("/{ip_address}")
def stream_camera(
    ip_address: str,
    fps: float = Query(DEFAULT_FPS, gt=0, le=30, description="Frames per second to relay"),
    max_width: int = Query(DEFAULT_MAX_WIDTH, ge=160, le=3840, description="Scale frames down to this width"),
):
    """Live MJPEG from one camera, by IP."""
    global _active_streams
    known_camera(ip_address)

    if _active_streams >= MAX_CONCURRENT_STREAMS:
        raise HTTPException(
            status_code=429,
            detail=f"Too many live streams open (limit {MAX_CONCURRENT_STREAMS})",
        )

    capture = open_capture(ip_address)
    return StreamingResponse(
        mjpeg_frames(capture, ip_address, fps, max_width),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )
