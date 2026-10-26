#!/usr/bin/env python3
"""
test-pic.py

Count what YOLO finds in a saved picture, and write an annotated copy so the
number can be checked against the boxes that produced it.

Usage:
    python test-pic.py                                  # captures/pic01.jpg
    python test-pic.py captures/pic01.jpg
    python test-pic.py --model yolov8s.pt --imgsz 1920 --confidence 0.25
    python test-pic.py --classes person,laptop
    python test-pic.py --no-save                        # count only
    python test-pic.py --sweep                          # compare settings

--sweep is the one to reach for when a count looks wrong: it runs the same
picture through a grid of weights, resolutions and thresholds and prints what
each combination finds, which is how you tell "the model is too small" from
"the threshold is too high".

Exit codes:
    0 - counted
    1 - image not found or unreadable
    2 - model failed to load
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2

from cctv_common import (
    PACKAGE_DIR,
    DetectionParams,
    detect,
    detect_tiled,
    draw_annotations,
    load_model,
    resolve_classes,
    resolve_device,
)

DEFAULT_IMAGE = PACKAGE_DIR / "captures" / "pic01.jpg"
# Weights, resolution and confidence worth trying when a count looks low.
SWEEP_WEIGHTS = ("yolov8n.pt", "yolov8s.pt")
SWEEP_IMGSZ = (640, 1280, 1920)
SWEEP_CONF = (0.5, 0.35, 0.25)


def load_image(path: Path):
    """Read an image, with a clear message for the two ways it can fail."""
    if not path.is_file():
        print(f"Image not found: {path}", file=sys.stderr)
        return None

    image = cv2.imread(str(path))
    if image is None:
        print(f"Could not decode image: {path} (unsupported format or corrupt)", file=sys.stderr)
        return None
    return image


def describe(detections) -> str:
    """Per-class totals, e.g. 'person 31, laptop 4'."""
    counts: dict[str, int] = {}
    for item in detections:
        counts[item.label] = counts.get(item.label, 0) + 1
    return ", ".join(f"{label} {n}" for label, n in sorted(counts.items())) or "nothing"


def run_sweep(image, device: str, classes, iou: float) -> None:
    """Count the same picture under a grid of settings and print the table."""
    print(f"\n{'weights':<12} {'imgsz':>6} {'conf':>6} {'count':>7} {'ms':>8}")
    print("-" * 44)

    for weights in SWEEP_WEIGHTS:
        try:
            model = load_model(weights, device)
        except RuntimeError as exc:
            print(f"{weights:<12} {'—':>6} {'—':>6} {'skipped':>7}   {exc}")
            continue

        for imgsz in SWEEP_IMGSZ:
            for conf in SWEEP_CONF:
                params = DetectionParams(
                    confidence=conf, iou=iou, imgsz=imgsz, device=device,
                    half=device.startswith("cuda"), classes=classes,
                )
                started = time.perf_counter()
                found = detect(image, model, params)
                elapsed = (time.perf_counter() - started) * 1000
                print(f"{weights:<12} {imgsz:>6} {conf:>6.2f} {len(found):>7} {elapsed:>8.0f}")

    print(
        "\nA count that climbs with imgsz means people are too small at the lower size;\n"
        "one that climbs as conf drops means they were found but discarded."
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Count people in a saved picture with YOLO.")
    parser.add_argument(
        "image", nargs="?", type=Path, default=DEFAULT_IMAGE,
        help=f"Picture to count (default: {DEFAULT_IMAGE.parent.name}/{DEFAULT_IMAGE.name}).",
    )
    parser.add_argument(
        "-o", "--out", type=Path, default=None,
        help="Where to write the annotated copy (default: alongside the input, suffixed _detected).",
    )
    parser.add_argument(
        "--save", dest="write_out", action=argparse.BooleanOptionalAction, default=True,
        help="Write the annotated copy (default: on; --no-save to count only).",
    )
    parser.add_argument("--model", default="yolov8n.pt", help="YOLO weights (default: yolov8n.pt).")
    parser.add_argument("--device", default="auto", help="auto, cpu, cuda, or a GPU index (default: auto).")
    parser.add_argument(
        "--classes", default=None,
        help="Names or ids, comma separated; 'all' for every class (default: person).",
    )
    parser.add_argument("--confidence", type=float, default=0.5, help="Minimum confidence (default: 0.5).")
    parser.add_argument("--iou", type=float, default=0.45, help="NMS IoU threshold (default: 0.45).")
    parser.add_argument("--imgsz", type=int, default=640, help="Inference resolution (default: 640).")
    parser.add_argument(
        "--tiles", type=int, default=0, metavar="N",
        help="Also detect on an NxN grid of overlapping crops and merge the results. "
             "Finds far more distant people, at N*N+1 times the inference cost.",
    )
    parser.add_argument(
        "--overlap", type=float, default=0.2,
        help="How much neighbouring tiles overlap, so nobody is split by a seam (default: 0.2).",
    )
    parser.add_argument(
        "--sweep", action="store_true",
        help="Compare weights, resolutions and thresholds on this picture.",
    )
    parser.add_argument("--quiet", action="store_true", help="Print only the count.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    image = load_image(args.image)
    if image is None:
        return 1

    height, width = image.shape[:2]
    device = resolve_device(args.device)

    if args.sweep:
        try:
            probe = load_model(SWEEP_WEIGHTS[0], device)
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 2
        run_sweep(image, device, resolve_classes(probe, args.classes), args.iou)
        return 0

    try:
        model = load_model(args.model, device)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2

    try:
        classes = resolve_classes(model, args.classes)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    params = DetectionParams(
        confidence=args.confidence, iou=args.iou, imgsz=args.imgsz,
        device=device, half=device.startswith("cuda"), classes=classes,
    )

    started = time.perf_counter()
    if args.tiles:
        found = detect_tiled(image, model, params, args.tiles, args.tiles, args.overlap)
    else:
        found = detect(image, model, params)
    elapsed = (time.perf_counter() - started) * 1000

    if args.quiet:
        print(len(found))
        return 0

    print(f"\n{args.image}  ({width}x{height})")
    print(f"  model      : {args.model} on {device}")
    tiling = f" tiles={args.tiles}x{args.tiles} overlap={args.overlap}" if args.tiles else ""
    print(f"  settings   : imgsz={params.imgsz} conf={params.confidence} iou={params.iou}{tiling}")
    print(f"  found      : {describe(found)}")
    print(f"  count      : {len(found)}")
    print(f"  took       : {elapsed:.0f} ms")

    if found:
        print("\n  #   class        conf   x     y     w     h")
        for i, item in enumerate(found, 1):
            x, y, w, h = item.box
            print(f"  {i:<3} {item.label:<12} {item.confidence:.2f}  {x:<5} {y:<5} {w:<5} {h:<5}")

    if args.write_out:
        out = args.out or args.image.with_name(f"{args.image.stem}_detected{args.image.suffix}")
        annotated = draw_annotations(image, found, f"Count: {len(found)}")
        if cv2.imwrite(str(out), annotated):
            print(f"\n  annotated  : {out}")
        else:
            print(f"\n  could not write {out}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
