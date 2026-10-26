"""
Shared pieces for the CCTV counters.

counter-person.py (the scheduled poller) and count_api.py (the HTTP service)
both need the same config loading, camera URL building and YOLO setup. Keeping
one copy means a change to how credentials are handled lands in both.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

# Force TCP: more stable than UDP on a busy network. FFmpeg reads this when
# VideoCapture opens, so it only has to be set before the first connection.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import cv2

if TYPE_CHECKING:  # ultralytics drags in torch; only type checkers need it here
    from ultralytics import YOLO

logger = logging.getLogger("cctv")

COCO_PERSON_CLASS_ID = 0
PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = PACKAGE_DIR / "config.json"
# The webcam counter already downloaded these; reuse them instead of pulling
# the same weights again into this folder.
SHARED_WEIGHTS_DIR = PACKAGE_DIR.parent / "face-counter"


@dataclass(frozen=True)
class DetectionParams:
    """Parameters controlling YOLO detection sensitivity."""

    confidence: float = 0.5           # minimum detection confidence to keep a box
    iou: float = 0.45                 # IoU threshold used for NMS
    imgsz: int = 640                  # inference resolution; raise it for distant people
    device: str = "cpu"               # "cpu", "cuda", or a GPU index like "0"
    half: bool = False                # FP16 inference; CUDA only, roughly 2x throughput
    classes: tuple[int, ...] | None = (COCO_PERSON_CLASS_ID,)  # None means every class

    def with_overrides(self, **changes) -> "DetectionParams":
        """A copy with the given fields replaced, ignoring any left as None."""
        return replace(self, **{k: v for k, v in changes.items() if v is not None})

    def with_classes(self, classes: tuple[int, ...] | None) -> "DetectionParams":
        """
        A copy detecting exactly these classes; None means every class.

        Separate from with_overrides because None is a real value here, not
        "leave it alone" — routing it through there would quietly turn a
        request for every class back into the person-only default.
        """
        return replace(self, classes=classes)


@dataclass(frozen=True)
class Detection:
    """One box YOLO returned."""

    class_id: int
    label: str
    confidence: float
    box: tuple[int, int, int, int]  # x, y, width, height

    def as_dict(self) -> dict:
        x, y, w, h = self.box
        return {
            "class_id": self.class_id,
            "label": self.label,
            "confidence": round(self.confidence, 4),
            "box": {"x": x, "y": y, "width": w, "height": h},
        }


def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> dict:
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


def resolve_device(requested: str) -> str:
    """
    Pick the torch device and say out loud which one YOLO will run on.

    Ultralytics falls back to the CPU silently when CUDA is missing, so a
    counter that quietly runs ten times slower looks exactly like one that is
    working. "auto" prefers the GPU; naming a device explicitly makes a missing
    GPU a loud warning rather than an invisible downgrade.
    """
    import torch  # imported here so --help works without loading torch

    available = torch.cuda.is_available()
    wanted_gpu = requested != "cpu"

    if wanted_gpu and available:
        index = 0 if requested in ("auto", "cuda") else int(requested)
        name = torch.cuda.get_device_name(index)
        memory = torch.cuda.get_device_properties(index).total_memory / 1024 ** 3
        logger.info("YOLO device: cuda:%d (%s, %.1f GiB)", index, name, memory)
        return f"cuda:{index}"

    if wanted_gpu and requested != "auto":
        # Explicitly asked for a GPU and there is none: worth shouting about.
        logger.error(
            "GPU requested but torch reports no CUDA device (torch %s, cuda build %s). "
            "Falling back to CPU.", torch.__version__, torch.version.cuda,
        )
    elif wanted_gpu:
        logger.warning(
            "No CUDA device available (torch %s, cuda build %s) — running on CPU.",
            torch.__version__, torch.version.cuda,
        )
    else:
        logger.info("YOLO device: cpu (requested)")

    return "cpu"


def load_model(weights: str, device: str = "cpu") -> "YOLO":
    """
    Load a YOLO model, downloading pretrained weights on first use.

    ultralytics is imported here rather than at module scope so that tools
    which only move pictures around — capture.py, say — do not pay ~1.7s and
    a few hundred MB of torch to use build_rtsp_url.
    """
    from ultralytics import YOLO

    try:
        model = YOLO(resolve_weights(weights))
        # Move the weights once here rather than per predict() call, so the
        # transfer does not land in the middle of a sampling loop.
        model.to(device)
        return model
    except Exception as exc:  # ultralytics raises plain Exception on bad weights
        raise RuntimeError(f"Failed to load YOLO model '{weights}': {exc}") from exc


def detect(frame, model: YOLO, params: DetectionParams) -> list[Detection]:
    """Every box YOLO finds in one frame, filtered to params.classes."""
    # half is only passed when it is on: ultralytics 8.4 deprecated the
    # argument and warns once per call, so always sending half=False filled
    # the log with a notice about a feature we were not using.
    extra = {"half": True} if params.half else {}
    results = model.predict(
        frame,
        classes=list(params.classes) if params.classes else None,
        conf=params.confidence,
        iou=params.iou,
        imgsz=params.imgsz,
        device=params.device,
        verbose=False,
        **extra,
    )

    names = results[0].names
    detections: list[Detection] = []
    for box in results[0].boxes:
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        class_id = int(box.cls[0])
        detections.append(Detection(
            class_id=class_id,
            label=names.get(class_id, str(class_id)),
            confidence=float(box.conf[0]),
            box=(int(x1), int(y1), int(x2 - x1), int(y2 - y1)),
        ))
    return detections


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Intersection over union of two (x, y, w, h) boxes."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    left, top = max(ax, bx), max(ay, by)
    right, bottom = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if right <= left or bottom <= top:
        return 0.0
    overlap = (right - left) * (bottom - top)
    return overlap / float(aw * ah + bw * bh - overlap)


def merge_detections(detections: list[Detection], iou_threshold: float = 0.5) -> list[Detection]:
    """
    Greedy non-max suppression across detections from different passes.

    Tiles overlap so that a person on a seam is whole in at least one of them,
    which means the same person is found more than once. Keeping the most
    confident box of each cluster turns those duplicates back into one count.
    """
    kept: list[Detection] = []
    for candidate in sorted(detections, key=lambda d: d.confidence, reverse=True):
        if any(
            other.class_id == candidate.class_id and _iou(other.box, candidate.box) > iou_threshold
            for other in kept
        ):
            continue
        kept.append(candidate)
    return kept


def detect_tiled(
    frame,
    model: YOLO,
    params: DetectionParams,
    rows: int = 2,
    cols: int = 2,
    overlap: float = 0.5,
    include_full_frame: bool = True,
) -> list[Detection]:
    """
    Detect on overlapping crops as well as the whole frame, then merge.

    YOLO resizes its input to imgsz, so on a wide CCTV shot a seated person a
    few desks away can end up only a dozen pixels tall and is simply not there
    to be found. Running the same model over crops means each crop is scaled
    up rather than down, and the same person arrives at a workable size.

    include_full_frame keeps a whole-frame pass in the mix, because anyone
    large enough to span several tiles is better found once than in pieces.
    """
    height, width = frame.shape[:2]
    tile_w, tile_h = width / cols, height / rows
    pad_x, pad_y = tile_w * overlap, tile_h * overlap

    found: list[Detection] = list(detect(frame, model, params)) if include_full_frame else []

    for row in range(rows):
        for col in range(cols):
            x1 = max(int(col * tile_w - pad_x), 0)
            y1 = max(int(row * tile_h - pad_y), 0)
            x2 = min(int((col + 1) * tile_w + pad_x), width)
            y2 = min(int((row + 1) * tile_h + pad_y), height)

            crop = frame[y1:y2, x1:x2]
            if crop.size == 0:
                continue

            for item in detect(crop, model, params):
                bx, by, bw, bh = item.box
                # Back to whole-frame coordinates, or every tile's boxes would
                # pile up in the top-left corner.
                found.append(replace(item, box=(bx + x1, by + y1, bw, bh)))

    return merge_detections(found, params.iou)


def count_persons(frame, model: YOLO, params: DetectionParams) -> int:
    """Number of people YOLO finds in one frame."""
    people = params.with_overrides(classes=(COCO_PERSON_CLASS_ID,))
    return len(detect(frame, model, people))


def resolve_classes(model: YOLO, requested: str | None) -> tuple[int, ...] | None:
    """
    Turn "person,laptop" or "0,63" into class ids.

    Accepts names and numbers so callers do not have to memorise the COCO
    ordering. "all" (or an empty value) means do not filter at all.

    Raises ValueError naming the unknown entry, so the API can hand the caller
    a 400 that says which one was wrong.
    """
    if requested is None or not requested.strip():
        return (COCO_PERSON_CLASS_ID,)

    if requested.strip().lower() == "all":
        return None

    by_name = {name.lower(): class_id for class_id, name in model.names.items()}
    resolved: list[int] = []
    for raw in requested.split(","):
        token = raw.strip().lower()
        if not token:
            continue
        if token.isdigit():
            class_id = int(token)
            if class_id not in model.names:
                raise ValueError(f"Unknown class id {class_id}")
            resolved.append(class_id)
        elif token in by_name:
            resolved.append(by_name[token])
        else:
            raise ValueError(f"Unknown class '{raw.strip()}'")

    return tuple(dict.fromkeys(resolved)) or (COCO_PERSON_CLASS_ID,)


def draw_annotations(frame, detections: list[Detection], label: str | None = None):
    """Return a copy of the frame with boxes drawn and an optional caption."""
    annotated = frame.copy()
    for detection in detections:
        x, y, w, h = detection.box
        cv2.rectangle(annotated, (x, y), (x + w, y + h), (0, 255, 0), 2)
        caption = f"{detection.label} {detection.confidence:.2f}"
        cv2.putText(
            annotated, caption, (x, max(y - 6, 12)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA,
        )

    if label:
        cv2.putText(
            annotated, label, (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2, cv2.LINE_AA,
        )
    return annotated


def build_rtsp_url(ip_address: str, config: dict) -> str | None:
    """
    Turn a camera IP into a full RTSP URL.

    Callers only know each room's camera IP; the credentials and stream path
    are the same across the cameras, so they come from config instead of being
    stored per room.
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
