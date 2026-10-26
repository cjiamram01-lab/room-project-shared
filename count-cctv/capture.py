#!/usr/bin/env python3
"""
capture.py

Save still pictures from the CCTV cameras to files. No detection, no model —
just grab frames and write JPEGs.

Usage:
    python capture.py 10.100.197.21                 # one shot
    python capture.py 10.100.197.21 10.100.197.20   # one shot from each
    python capture.py --all                         # every camera in ip_binding
    python capture.py --room 27.03.04               # every camera in one room
    python capture.py 10.100.197.21 -o desk.jpg     # a specific filename
    python capture.py --all --count 12 --interval 300   # hourly-ish time lapse
    python capture.py --all --max-width 960 --quality 70

Camera credentials come from the environment or config.json:
    set CAM_USER=admin & set CAM_PASSWORD=secret      (Windows)
    export CAM_USER=admin CAM_PASSWORD=secret         (Linux)

Exit codes:
    0 - every requested capture succeeded
    1 - nothing to capture (no cameras resolved, or no credentials)
    2 - at least one camera failed
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import requests

from cctv_common import (
    DEFAULT_CONFIG_PATH,
    PACKAGE_DIR,
    build_rtsp_url,
    load_config,
    mask_url,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("capture")

DEFAULT_OUTPUT_DIR = PACKAGE_DIR / "captures"
DEFAULT_API_BASE = "http://localhost:8000"
CAMERAS_PATH = "/camera-stream/cameras"


def fetch_cameras(api_base: str, room: str | None, timeout: float) -> list[tuple[str, str]]:
    """
    Ask control-api which cameras exist, as (room_no, ip_address) pairs.

    The bindings live in the database rather than in this script's config, so
    adding a camera to a room does not mean editing anything here.
    """
    url = api_base.rstrip("/") + CAMERAS_PATH
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        rows = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.error("Could not fetch the camera list from %s: %s", url, exc)
        return []

    cameras = [
        (str(row.get("room_no") or "unknown"), str(row["ip_address"]).strip())
        for row in rows
        if isinstance(row, dict) and row.get("ip_address")
    ]
    if room:
        cameras = [c for c in cameras if c[0] == room]
        if not cameras:
            logger.error("No camera registered for room %s", room)
    return cameras


def grab_frame(url: str, warmup: int, timeout: float):
    """
    Open the stream and return one usable frame, or None.

    The first frames after an RTSP connect are usually partial while the
    decoder waits for a keyframe, so a few are taken and thrown away; the
    last one read is the one kept.
    """
    capture = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not capture.isOpened():
        capture.release()
        logger.error("Could not connect to %s", mask_url(url))
        return None

    capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    deadline = time.monotonic() + timeout
    frame = None

    try:
        for _ in range(max(warmup, 1)):
            if time.monotonic() > deadline:
                break
            ok, candidate = capture.read()
            if ok:
                frame = candidate
            else:
                # read() normally blocks; a stream that errors returns at once,
                # so without this the loop would spin until the deadline.
                time.sleep(0.1)
    finally:
        capture.release()

    return frame


def output_path(base: Path, room: str, ip_address: str, explicit: Path | None) -> Path:
    """Where a capture is written: the given name, or filed by room and day."""
    if explicit is not None:
        return explicit

    stamp = datetime.now()
    folder = base / room / stamp.strftime("%Y-%m-%d")
    return folder / f"{stamp.strftime('%H%M%S')}_{ip_address}.jpg"


def save_frame(frame, path: Path, max_width: int, quality: int) -> bool:
    """Scale if asked, write the JPEG, and report whether it landed."""
    if max_width and frame.shape[1] > max_width:
        scale = max_width / frame.shape[1]
        frame = cv2.resize(frame, (max_width, int(frame.shape[0] * scale)))

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, quality]):
            logger.error("Could not write %s", path)
            return False
    except OSError as exc:
        logger.error("Could not write %s: %s", path, exc)
        return False

    size_kb = path.stat().st_size / 1024
    logger.info("Saved %s (%dx%d, %.0f KB)", path, frame.shape[1], frame.shape[0], size_kb)
    return True


def capture_round(cameras: list[tuple[str, str]], config: dict, args: argparse.Namespace) -> int:
    """Capture one picture from each camera; returns how many failed."""
    failed = 0
    for room, ip_address in cameras:
        url = build_rtsp_url(ip_address, config)
        if url is None:
            logger.error("No camera credentials, cannot build a URL for %s", ip_address)
            failed += 1
            continue

        frame = grab_frame(url, args.warmup, args.timeout)
        if frame is None:
            logger.error("room=%s %s gave no usable frame", room, ip_address)
            failed += 1
            continue

        # -o names a single file, so it only makes sense for a single capture;
        # with several cameras each would overwrite the last.
        explicit = args.output if (args.output and len(cameras) == 1) else None
        if not save_frame(frame, output_path(args.dir, room, ip_address, explicit),
                          args.max_width, args.quality):
            failed += 1

    return failed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Save still pictures from the CCTV cameras.",
        epilog="Give camera IPs, or use --all / --room to look them up in ip_binding.",
    )
    parser.add_argument("ips", nargs="*", help="Camera IP addresses to capture from.")
    parser.add_argument("--all", action="store_true", help="Capture from every camera in ip_binding.")
    parser.add_argument("--room", default=None, help="Capture from every camera bound to this room.")
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Write to this exact file (single camera only).",
    )
    parser.add_argument(
        "-d", "--dir", type=Path, default=DEFAULT_OUTPUT_DIR,
        help=f"Directory to file captures under, by room and day (default: {DEFAULT_OUTPUT_DIR.name}/).",
    )
    parser.add_argument("--count", type=int, default=1, help="How many rounds to capture (default: 1).")
    parser.add_argument(
        "--interval", type=float, default=0.0,
        help="Seconds between rounds when --count > 1 (default: 0).",
    )
    parser.add_argument(
        "--warmup", type=int, default=5,
        help="Frames to read before keeping one, to let the decoder settle (default: 5).",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="Seconds to spend on one camera (default: 15).")
    parser.add_argument(
        "--max-width", type=int, default=0,
        help="Scale frames down to this width; 0 keeps the camera's own size (default: 0).",
    )
    parser.add_argument("--quality", type=int, default=90, choices=range(1, 101), metavar="1-100",
                        help="JPEG quality (default: 90).")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to config.json.")
    parser.add_argument(
        "--api-base", default=None,
        help=f"control-api base URL for --all / --room (default: api_base from config, else {DEFAULT_API_BASE}).",
    )
    parser.add_argument("--api-timeout", type=float, default=10.0, help="Seconds to wait on the API (default: 10).")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)

    if args.ips:
        cameras = [("manual", ip.strip()) for ip in args.ips if ip.strip()]
    elif args.all or args.room:
        api_base = args.api_base or config.get("api_base") or DEFAULT_API_BASE
        cameras = fetch_cameras(api_base, args.room, args.api_timeout)
    else:
        logger.error("Give one or more camera IPs, or use --all / --room.")
        return 1

    if not cameras:
        return 1

    # Fail now rather than once per camera.
    if build_rtsp_url("0.0.0.0", config) is None:
        logger.error(
            "No camera credentials. Set CAM_USER and CAM_PASSWORD, or add "
            'rtsp_user/rtsp_password (or rtsp_template) to %s.', args.config
        )
        return 1

    logger.info("Capturing from %d camera(s)", len(cameras))
    failed = 0
    try:
        for round_no in range(max(args.count, 1)):
            if round_no:
                time.sleep(max(args.interval, 0))
            if args.count > 1:
                logger.info("Round %d of %d", round_no + 1, args.count)
            failed += capture_round(cameras, config, args)
    except KeyboardInterrupt:
        logger.info("Interrupted, stopping.")

    if failed:
        logger.warning("%d capture(s) failed", failed)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
