#!/usr/bin/env python3
"""Prepare an auditable ChatGPT visual review from a saved candidate record."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def load_candidate(run_dir: Path, candidate_id: str) -> dict:
    ledger = run_dir / "CANDIDATE_LEDGER.jsonl"
    if not ledger.is_file():
        raise FileNotFoundError(f"Candidate ledger not found: {ledger}")
    for line in ledger.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        candidate = json.loads(line)
        if candidate.get("candidate_id") == candidate_id:
            return candidate
    raise ValueError(f"Candidate ID not found in {ledger}: {candidate_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    output_dir = args.output_dir.resolve()
    candidate = load_candidate(run_dir, args.candidate_id)
    screenshot_record = candidate.get("screenshots") or {}
    output_dir.mkdir(parents=True, exist_ok=True)
    images: list[dict] = []
    viewport_crop_heights = {"desktop": 900, "mobile": 844}
    for viewport in ("desktop", "mobile"):
        record = screenshot_record.get(viewport) or {}
        if record.get("status") != "saved" or not record.get("path"):
            raise ValueError(f"Candidate {args.candidate_id} has no saved {viewport} screenshot")
        source = (Path(record["path"]) if Path(record["path"]).is_absolute()
                  else run_dir.parent.parent / Path(record["path"]))
        source = source.resolve()
        try:
            source.relative_to(run_dir.parent.parent.resolve())
        except ValueError as exc:
            raise ValueError(f"Screenshot path escapes the repository: {source}") from exc
        if not source.is_file() or source.suffix.lower() not in IMAGE_SUFFIXES:
            raise FileNotFoundError(f"Saved {viewport} screenshot is unavailable: {source}")
        target = output_dir / f"{viewport}.jpg"
        with Image.open(source) as image:
            original_size = image.size
            crop_height = min(viewport_crop_heights[viewport], image.height)
            image.crop((0, 0, image.width, crop_height)).convert("RGB").save(
                target, format="JPEG", quality=88, optimize=True
            )
        images.append({"viewport": viewport, "path": str(target), "filename": target.name,
                       "source_path": record["path"], "bytes": target.stat().st_size,
                       "source_dimensions": {"width": original_size[0], "height": original_size[1]},
                       "uploaded_dimensions": {"width": image.width, "height": crop_height},
                       "crop": "top viewport crop from saved full-page screenshot"})

    context = {
        "candidate_id": candidate.get("candidate_id"),
        "business_name": candidate.get("business_name"),
        "website_url": candidate.get("final_url") or candidate.get("canonical_url"),
        "site_title": candidate.get("site_title"),
        "screenshots": [{"viewport": item["viewport"], "filename": item["filename"]}
                        for item in images],
    }
    instructions = """You are reviewing a local business website as a visual redesign-opportunity assessor.
Use only what is visible in the supplied desktop and mobile screenshots. Do not browse the site or treat the supplied title/URL as visual evidence. Do not infer facts that screenshots cannot establish (such as current business status, licensing, technology, or whether forms work). If either screenshot is missing, obstructed, too small, or not interpretable, set verdict to uncertain and score to null.

Score redesign opportunity with an integer from 0 to 100: higher means stronger visual evidence that a redesign could materially improve the site. Use high for one severe flaw or several major flaws that materially harm trust, readability, navigation, mobile usability, or the ability to request service. Use medium when the site is functional and credible but has a meaningful opportunity such as dated styling, weak hierarchy, sparse/poor project photos, text-heavy layout, or confusing navigation. Use pass when it appears credible, usable, reasonably current, and presents services/contact clearly; minor cosmetic issues alone are insufficient. A dated look and mobile failure do not both need to be present for high. Score bands: high=70-100, medium=35-69, pass=0-34. Use uncertain with a null score when evidence is insufficient.

Return only valid JSON with exactly these keys: candidate_id, website_url, visual_opportunity_score, visual_verdict, confidence, desktop_observations, mobile_observations, major_issues, positive_signals, recommended_redesign_focus, limitations. candidate_id and website_url must match the supplied context. visual_verdict must be high, medium, pass, or uncertain. confidence must be low, medium, or high. The two observations fields, major_issues, positive_signals, recommended_redesign_focus, and limitations must be arrays of concise strings. Anchor each observation to visible evidence. Do not claim a check succeeded unless the screenshots show that evidence."""
    prompt = "Assess the supplied desktop and mobile screenshots and score the visual redesign opportunity under the rubric. Treat the attachment named desktop.* as the desktop screenshot and mobile.* as the mobile screenshot. Use the candidate context only for identity; base the score on visible screenshot evidence."
    (output_dir / "candidate-context.json").write_text(json.dumps(context, indent=2) + "\n", encoding="utf-8")
    (output_dir / "instructions.txt").write_text(instructions + "\n", encoding="utf-8")
    (output_dir / "prompt.txt").write_text(prompt + "\n", encoding="utf-8")
    (output_dir / "input-manifest.json").write_text(json.dumps({"context": context, "images": images}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output_dir": str(output_dir), "candidate": context, "images": images}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
