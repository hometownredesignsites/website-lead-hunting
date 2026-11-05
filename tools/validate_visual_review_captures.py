#!/usr/bin/env python3
"""Reject visual-review inputs whose mid views do not represent real scrolling."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageChops


MIN_MID_SCROLL_PX = 150
VIEWPORTS = ("desktop", "mobile")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def image_file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate(manifest_path: Path, recapture_manifest_path: Path | None = None,
             minimum_scroll_px: int = MIN_MID_SCROLL_PX) -> list[str]:
    manifest = read_json(manifest_path)
    images = {(item.get("viewport"), item.get("view")): item
              for item in manifest.get("images", []) if isinstance(item, dict)}
    errors: list[str] = []

    recaptured = {}
    if recapture_manifest_path:
        recapture_manifest = read_json(recapture_manifest_path)
        recaptured = {item.get("viewport"): item
                      for item in recapture_manifest.get("screenshots", [])
                      if isinstance(item, dict)}

    for viewport in VIEWPORTS:
        top = images.get((viewport, "top"))
        mid = images.get((viewport, "mid"))
        if not top or not mid:
            errors.append(f"{viewport}: top or mid screenshot is missing from the input manifest")
            continue

        offset = int(mid.get("source_top_px") or 0)
        scrollable_height = int(mid.get("scrollable_height_px") or 0)
        if offset < minimum_scroll_px:
            errors.append(
                f"{viewport}: mid crop moved only {offset}px (needs at least {minimum_scroll_px}px); "
                f"source scroll range was {scrollable_height}px"
            )

        top_path = Path(top.get("path", ""))
        mid_path = Path(mid.get("path", ""))
        if not top_path.is_file() or not mid_path.is_file():
            errors.append(f"{viewport}: top or mid screenshot file is missing")
        elif image_file_hash(top_path) == image_file_hash(mid_path):
            errors.append(f"{viewport}: top and mid screenshots are byte-for-byte duplicates")
        else:
            try:
                with Image.open(top_path) as top_image, Image.open(mid_path) as mid_image:
                    if top_image.size != mid_image.size:
                        errors.append(f"{viewport}: top and mid screenshot dimensions differ")
                    elif ImageChops.difference(top_image.convert("RGB"), mid_image.convert("RGB")).getbbox() is None:
                        errors.append(f"{viewport}: top and mid screenshots are visually identical")
            except OSError as exc:
                errors.append(f"{viewport}: screenshot image could not be decoded ({type(exc).__name__})")

        if recapture_manifest_path:
            capture = recaptured.get(viewport) or {}
            scroll_check = capture.get("scroll_validation") or {}
            if not scroll_check.get("verified"):
                actual = int(scroll_check.get("max_observed_scroll_px") or 0)
                required = int(scroll_check.get("minimum_required_scroll_px") or minimum_scroll_px)
                errors.append(
                    f"{viewport}: browser scroll was not verified ({actual}px observed; {required}px required)"
                )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True,
                        help="input-manifest.json written by prepare_chatgpt_visual_review.py")
    parser.add_argument("--recapture-manifest", type=Path,
                        help="recapture-manifest.json when screenshots were freshly captured")
    parser.add_argument("--minimum-scroll-px", type=int, default=MIN_MID_SCROLL_PX)
    args = parser.parse_args()

    errors = validate(args.manifest, args.recapture_manifest, args.minimum_scroll_px)
    if errors:
        print("Visual screenshot scroll validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"Verified distinct desktop and mobile views with at least {args.minimum_scroll_px}px of scrolling.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
