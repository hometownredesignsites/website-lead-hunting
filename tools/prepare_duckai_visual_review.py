#!/usr/bin/env python3
"""Prepare an auditable Duck.ai visual review from a saved candidate record."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
DESKTOP_MIN_WIDTH = 768


def validate_source_viewport(viewport: str, width: int, height: int, source: Path) -> None:
    if width <= 0 or height <= 0:
        raise ValueError(f"{viewport} screenshot has invalid dimensions: {source}")
    if viewport == "desktop" and width < DESKTOP_MIN_WIDTH:
        raise ValueError(f"Desktop screenshot appears mismatched (width {width}px): {source}")
    if viewport == "mobile" and width >= DESKTOP_MIN_WIDTH:
        raise ValueError(f"Mobile screenshot appears mismatched (width {width}px): {source}")


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


def resolve_screenshot(record_path: str, run_dir: Path) -> Path | None:
    """Resolve only screenshots contained in the downloaded lead-hunt artifact."""
    normalized = record_path.replace("\\", "/").strip()
    parts = [part for part in normalized.split("/") if part and part != "."]
    if len(parts) >= 3 and parts[0] == "runs" and parts[1] == run_dir.name:
        parts = parts[2:]
    elif len(parts) >= 2 and parts[0].endswith(":"):
        try:
            marker = parts.index("runs")
            if parts[marker + 1] != run_dir.name:
                return None
            parts = parts[marker + 2:]
        except (ValueError, IndexError):
            return None
    if not parts:
        return None
    try:
        resolved = (run_dir / Path(*parts)).resolve()
        resolved.relative_to(run_dir.resolve())
    except (OSError, ValueError):
        return None
    if resolved.is_file() and resolved.suffix.lower() in IMAGE_SUFFIXES:
        return resolved
    return None


def make_review_sheet(viewport: str, views: list[dict], output_dir: Path) -> dict:
    """Stack a viewport's labeled top and mid crops into one Duck.ai upload."""
    ordered = sorted(views, key=lambda item: (0 if item["view"] == "top" else 1))
    if [item["view"] for item in ordered] != ["top", "mid"]:
        raise ValueError(f"Expected top and mid screenshots for {viewport}")
    images = [Image.open(item["path"]).convert("RGB") for item in ordered]
    width = max(image.width for image in images)
    banner = 42
    gap = 12
    height = sum(image.height + banner for image in images) + gap
    sheet = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    y = 0
    covers = []
    for index, (item, image) in enumerate(zip(ordered, images)):
        label = f"{viewport.upper()} — {item['view'].upper()} VIEW"
        draw.rectangle((0, y, width, y + banner), fill=(28, 42, 62))
        draw.text((16, y + 8), label, fill="white", font=font)
        y += banner
        sheet.paste(image, (0, y))
        y += image.height + (gap if index == 0 else 0)
        covers.append(item["filename"])
    target = output_dir / f"{viewport}_review_sheet.jpg"
    sheet.save(target, format="JPEG", quality=88, optimize=True)
    for image in images:
        image.close()
    return {
        "viewport": viewport,
        "filename": target.name,
        "path": str(target),
        "covers": covers,
        "dimensions": {"width": sheet.width, "height": sheet.height},
        "bytes": target.stat().st_size,
    }


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
    mid_page_scroll_fractions = {"desktop": 0.35, "mobile": 0.60}
    for viewport in ("desktop", "mobile"):
        record = screenshot_record.get(viewport) or {}
        if record.get("status") != "saved" or not record.get("path"):
            raise ValueError(f"Candidate {args.candidate_id} has no saved {viewport} screenshot")
        source = resolve_screenshot(record["path"], run_dir)
        if source is None:
            raise FileNotFoundError(
                f"Saved {viewport} screenshot is unavailable in the downloaded lead-hunt artifact"
            )
        with Image.open(source) as image:
            original_size = image.size
            validate_source_viewport(viewport, image.width, image.height, source)
            crop_height = min(viewport_crop_heights[viewport], image.height)
            max_scroll = max(0, image.height - crop_height)
            mid_page_scroll_fraction = mid_page_scroll_fractions[viewport]
            view_positions = {
                "top": 0,
                "mid": round(max_scroll * mid_page_scroll_fraction),
            }
            for view, top in view_positions.items():
                target = output_dir / f"{viewport}_{view}.jpg"
                image.crop((0, top, image.width, top + crop_height)).convert("RGB").save(
                    target, format="JPEG", quality=90, optimize=True
                )
                images.append({
                    "viewport": viewport,
                    "view": view,
                    "path": str(target),
                    "filename": target.name,
                    "source_path": record["path"],
                    "bytes": target.stat().st_size,
                    "source_dimensions": {"width": original_size[0], "height": original_size[1]},
                    "uploaded_dimensions": {"width": image.width, "height": crop_height},
                    "source_top_px": top,
                    "scrollable_height_px": max_scroll,
                    "crop": ("top viewport crop from saved full-page screenshot" if view == "top"
                             else f"viewport crop at {mid_page_scroll_fraction:.0%} of the scrollable page"),
                })

    context = {
        "candidate_id": candidate.get("candidate_id"),
        "business_name": candidate.get("business_name"),
        "website_url": candidate.get("final_url") or candidate.get("canonical_url"),
        "site_title": candidate.get("site_title"),
        "screenshots": [{"viewport": item["viewport"], "view": item["view"],
                         "filename": item["filename"],
                         "source_top_px": item["source_top_px"],
                         "scrollable_height_px": item["scrollable_height_px"]}
                        for item in images],
    }
    # Upload each viewport crop independently so Duck.ai can inspect the same
    # full-resolution evidence as the ChatGPT review workflow.
    upload_images = [{
        "viewport": item["viewport"],
        "view": item["view"],
        "filename": item["filename"],
        "covers": [item["filename"]],
        "dimensions": item["uploaded_dimensions"],
    } for item in images]
    context["upload_images"] = [{key: image[key] for key in ("viewport", "filename", "covers", "dimensions")}
                                for image in upload_images]
    instructions = """You are reviewing a local business website as a visual redesign-opportunity assessor.
Use only the four supplied full-resolution screenshots: desktop_top.jpg, desktop_mid.jpg, mobile_top.jpg, and mobile_mid.jpg. The top views show the start of each page; the mid views are viewport crops taken at 35% of the desktop and 60% of the mobile screenshot's scrollable height. These are representative page views, not guaranteed semantic sections. Do not browse the site or treat the supplied title/URL as visual evidence. Do not infer facts that screenshots cannot establish (such as current business status, licensing, technology, or whether forms work). If an image is missing, obstructed, too small, or not interpretable, lower confidence and state the limitation; use an uncertain verdict and null score only when the combined evidence is insufficient.

Score redesign opportunity with an integer from 0 to 100: higher means stronger visual evidence that a redesign could materially improve the site. Assess the material impact of the visible issues, not how many refinements you can suggest. A polished, credible site with strong relevant imagery, clear services/contact, and usable desktop/mobile layouts is pass when its remaining observations are preferences or minor polish.

Use pass (0-49) when no clearly evidenced material weakness is likely to harm trust, comprehension, readability, navigation, mobile use, or the ability to request service. Small type, generous spacing, repeated components, restrained styling, or icon-based navigation are not by themselves meaningful redesign opportunities. You may mention these as minor refinements, but do not let them raise the score into medium.

Use medium (50-74) only when the screenshots show either one severe, clearly demonstrated weakness or at least two independent moderate weaknesses that together materially reduce usability, trust, comprehension, or conversion. For each weakness, name the specific affected viewport and the observable user impact (for example, text that cannot be read, a service/contact action that cannot be found, or a layout that prevents practical mobile use). A hypothetical or hedged impact such as “may hinder” is not sufficient. Small or gray text is not a readability failure unless the screenshot shows that users would struggle to read it. Do not treat whitespace, repeated section styling, icon-only navigation, or other cosmetic preferences as material by themselves. If the only supported findings are minor refinements or speculative impacts, the score must be 0-49 and the verdict must be pass.

Do not infer missing or hidden content, poor usability, or a broken section from a dark or sparse-looking area alone. If the screenshot does not clearly show content obscured, unreadable text, an obstructed control, or another concrete user-facing problem, describe the ambiguous appearance as a limitation and do not count it toward the score.

Use high (75-100) only when the evidence-cluster calculation reaches 75; examples are two severe clusters, four moderate clusters, or one severe plus two moderate clusters. Use uncertain with a null score when the combined evidence is insufficient. Score bands: high=75-100, medium=50-74, pass=0-49.

Calibrate the number mechanically from independently evidenced issue clusters. First count moderate clusters (M) and severe clusters (S); do not count cosmetic refinements, repeat observations of the same root problem, speculative effects, or the same text-size complaint across several screenshots more than once. Use score = min(100, 24 + 13*M + 26*S). This makes no material issue 24, one moderate 37, two moderate 50, one severe 50, three moderate 63, and two severe 76. Verdict must follow the score bands above. To count a moderate cluster, name a visible user-facing consequence; to count severe, show an obvious obstruction or substantial loss of readability, navigation, trust, or ability to request service. A dark panel, compact appearance, or small-looking text on a reduced image does not qualify by itself. If legibility is uncertain at the supplied image resolution, call it out as a limitation and do not score it as a material issue.

Return a concise plain-text assessment, not JSON. Use this exact labeled format so the result can be converted reliably:
Visual opportunity score: <integer 0-100>/100, or "uncertain (no score)" when the visual evidence is insufficient
Verdict: <high|medium|pass|uncertain>
Confidence: <low|medium|high>
Desktop top observations:
- <one concise observation tied to desktop_top.jpg>
Desktop mid observations:
- <one concise observation tied to desktop_mid.jpg>
Mobile top observations:
- <one concise observation tied to mobile_top.jpg>
Mobile mid observations:
- <one concise observation tied to mobile_mid.jpg>
Desktop observations:
- <summary across both desktop views>
Mobile observations:
- <summary across both mobile views>
Major issues:
- <issue, or "None observed">
Positive signals:
- <signal, or "None observed">
Recommended redesign focus:
- <focus area, or "None identified">
Limitations:
- <limitation, or "None noted">
Use at least one concise bullet in every section. Do not add a preamble. Keep the explicit score and verdict near the top. The Python workflow will normalize this labeled response into JSON. Do not claim a check succeeded unless the screenshots show that evidence."""
    prompt = "Assess all four supplied screenshots and score the visual redesign opportunity under the rubric. Match filenames to the viewport and view names in the supplied context. Use the candidate context only for identity; base the score on visible screenshot evidence across the top and mid-page views. Return the concise labeled text format requested in the instructions."
    (output_dir / "candidate-context.json").write_text(json.dumps(context, indent=2) + "\n", encoding="utf-8")
    (output_dir / "instructions.txt").write_text(instructions + "\n", encoding="utf-8")
    (output_dir / "prompt.txt").write_text(prompt + "\n", encoding="utf-8")
    (output_dir / "input-manifest.json").write_text(json.dumps({"context": context, "images": images}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output_dir": str(output_dir), "candidate": context,
                      "images": images, "upload_images": upload_images}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
