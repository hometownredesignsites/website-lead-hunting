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


def _compact(value, depth: int = 0):
    if depth > 5:
        return "[truncated]"
    if isinstance(value, str):
        return value[:350] + ("…" if len(value) > 350 else "")
    if isinstance(value, dict):
        omitted = {"html", "body", "stack", "traceback"}
        return {str(key): _compact(item, depth + 1)
                for key, item in list(value.items())[:24] if str(key).casefold() not in omitted}
    if isinstance(value, (list, tuple)):
        items = list(value)
        return [_compact(item, depth + 1) for item in items[:10]] + (
            [f"[{len(items) - 10} more items omitted]"] if len(items) > 10 else [])
    return value


def _limited(items, limit: int = 10) -> list:
    return _compact(list(items or [])[:limit])


def script_audit_summary(candidate: dict) -> dict:
    """Keep deterministic audit evidence structured and separate from visual scoring."""
    evidence = candidate.get("evidence") or {}
    checks = candidate.get("site_checks") or evidence.get("site_checks") or {}
    signals = candidate.get("signals") or {}
    hero_media = candidate.get("hero_media") or signals.get("hero_media") or evidence.get("hero_media") or {}
    http = checks.get("http_links_and_resources") or {}
    images = checks.get("broken_images") or {}
    forms = checks.get("contact_form_privacy") or {}
    links = checks.get("broken_links_and_interactions") or {}
    interactions = links.get("interactions") or {}
    email = checks.get("contact_email_opportunity") or {}
    summary = {
        "status": checks.get("status", "not_run"),
        "automated_priority": candidate.get("automated_priority"),
        "website_opportunity_score": candidate.get("website_opportunity_score"),
        "evidence_confidence": candidate.get("evidence_confidence"),
        "hero_media": hero_media,
        "signals": signals,
        "opportunity_signals": candidate.get("opportunity_signals", {}),
        "http_links_and_resources": {
            "status": http.get("status", "not_run"),
            "http_link_count": http.get("http_link_count", 0),
            "insecure_resource_count": http.get("insecure_resource_count", 0),
            "http_links": _limited(http.get("http_links")),
            "insecure_resources": _limited(http.get("insecure_resources")),
        },
        "broken_images": {
            "status": images.get("status", "not_run"),
            "failed_image_count": images.get("failed_image_count", 0),
            "lazy_loading": images.get("lazy_loading", {}),
            "images": _limited(images.get("images")),
            "request_failures": _limited(images.get("request_failures")),
        },
        "contact_form_privacy": {
            "status": forms.get("status", "not_run"),
            "form_detected": forms.get("form_detected"),
            "form_count": forms.get("form_count"),
            "privacy_policy_link_found": forms.get("privacy_policy_link_found"),
            "privacy_policy_links": _limited(forms.get("privacy_policy_links")),
        },
        "contact_email_opportunity": {
            "status": email.get("status", "not_run"),
            "consumer_provider_domain_mismatch": email.get("consumer_provider_domain_mismatch"),
            "opportunity_bonus_points": email.get("opportunity_bonus_points"),
        },
        "broken_links_and_interactions": {
            "status": links.get("status", "not_run"),
            "internal_links_checked": links.get("internal_links_checked", 0),
            "broken_internal_links": _limited(links.get("broken_internal_links")),
            "broken_same_page_anchors": _limited(links.get("broken_same_page_anchors")),
            "interactions": {
                "status": interactions.get("status", "not_run"),
                "tested_count": interactions.get("tested_count", 0),
                "findings": _limited(interactions.get("findings")),
            },
            "console_errors": _limited(links.get("console_errors")),
            "request_failures": _limited(links.get("request_failures")),
        },
        "latest_blog_post": checks.get("latest_blog_post", {"status": "not_run"}),
        "content_freshness": checks.get("content_freshness", {"status": "not_run"}),
        "copyright_footer_signal": checks.get("copyright_footer_signal", {"status": "not_run"}),
        "legacy_twitter_branding": checks.get("legacy_twitter_branding", {"status": "not_run"}),
    }
    return _compact(summary)


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
        "script_audit": script_audit_summary(candidate),
        "screenshots": [{"viewport": item["viewport"], "view": item["view"],
                         "filename": item["filename"],
                         "source_top_px": item["source_top_px"],
                         "scrollable_height_px": item["scrollable_height_px"]}
                        for item in images],
    }
    # Duck.ai currently accepts at most three images in a chat. Keep both
    # desktop crops at full resolution and combine the two mobile crops into
    # one labeled sheet so all four source views fit within that limit.
    mobile_sheet = make_review_sheet(
        "mobile", [item for item in images if item["viewport"] == "mobile"], output_dir
    )
    upload_images = [{
        "viewport": item["viewport"],
        "view": item["view"],
        "filename": item["filename"],
        "covers": [item["filename"]],
        "dimensions": item["uploaded_dimensions"],
    } for item in images if item["viewport"] == "desktop"]
    upload_images.append({
        "viewport": "mobile",
        "view": "top_and_mid",
        "filename": mobile_sheet["filename"],
        "covers": mobile_sheet["covers"],
        "dimensions": mobile_sheet["dimensions"],
    })
    context["upload_images"] = [{key: image[key] for key in ("viewport", "filename", "covers", "dimensions")}
                                for image in upload_images]
    instructions = """You are reviewing a local business website as a visual redesign-opportunity assessor.
Use the three supplied image files for visual judgments: desktop_top.jpg, desktop_mid.jpg, and mobile_review_sheet.jpg (which contains labeled mobile TOP and MID views). The mid views are representative viewport crops, not guaranteed semantic sections. A structured script audit is also supplied. Treat it as automated evidence, not as something visually verified. Use screenshots only for hero imagery, readability, and layout/mobile use. Use script audit evidence only for broken images and interactions. Keep evidence sources explicit and never describe an automated finding as visually observed. Do not browse the site or infer facts not shown in the screenshots or audit. If a required factor cannot be assessed from its evidence source, mark it not_assessable; the durable result will be uncertain instead of silently lowering the score.

Rate these five fixed factors, using exactly one severity per factor: none, minor, moderate, severe, or not_assessable. Give concise specific evidence and identify the evidence source. Factors: hero_imagery (screenshots), readability (screenshots), layout_mobile (screenshots), broken_images (script_audit), interactions (script_audit). Do not create extra scoring factors or count issue sentences. The durable score is calculated by Python from factor severities: none=0, minor=5, moderate=25, severe=50; sum and cap at 100. Verdict bands are pass 0-49, medium 50-74, high 75-100. `not_assessable` in any required factor makes the durable verdict uncertain with a null score. Missing factor entries or evidence make the scorecard invalid and trigger retry. Report your own score and verdict separately; they are retained only as model output.

Use none when the factor is supported and no material weakness is present. Use minor for polish with little user impact; moderate for a clear but bounded user impact; severe for an obvious substantial loss of readability, navigation, trust, or ability to use the site. Do not rate a factor from an unrelated issue, duplicate the same root cause across factors, or use speculative impact. A missing hero image can be moderate or severe only when screenshots establish that the first viewport lacks useful visual presentation; do not infer image quality from code.

For visual factors, cite the affected screenshot/view and concrete user impact. A hypothetical or hedged impact such as “may hinder” is not sufficient. Small or gray text is not a readability failure unless the screenshot shows that users would struggle to read it. Do not treat whitespace, repeated section styling, icon-only navigation, or other cosmetic preferences as material by themselves.

Do not infer missing or hidden content, poor usability, or a broken section from a dark or sparse-looking area alone. If the screenshot does not clearly show content obscured, unreadable text, an obstructed control, or another concrete user-facing problem, describe the ambiguous appearance as a limitation and do not count it toward the score.

Use uncertain with a null score when a required factor cannot be assessed; otherwise let the fixed factor severities determine the durable score and verdict.

Keep issue descriptions for explanation; they do not contribute directly to the durable score. The five factor ratings are the only inputs to that score.

Return a concise plain-text assessment, not JSON. Use this exact labeled format so the result can be converted reliably:
Visual opportunity score: <integer 0-100>/100, or "uncertain (no score)" when the visual evidence is insufficient
Verdict: <high|medium|pass|uncertain>
Confidence: <low|medium|high>
Desktop top observations:
- <one concise observation tied to desktop_top.jpg>
Desktop mid observations:
- <one concise observation tied to desktop_mid.jpg>
Mobile top observations:
- <one concise observation tied to the TOP panel of mobile_review_sheet.jpg>
Mobile mid observations:
- <one concise observation tied to the MID panel of mobile_review_sheet.jpg>
Desktop observations:
- <summary across both desktop views>
Mobile observations:
- <summary across both mobile views>
Major issues:
- <issue, or "None observed">
Moderate issue clusters:
- <one independently evidenced moderate user-impact issue per bullet, or "None observed">
Severe issue clusters:
- <one independently evidenced severe user-impact issue per bullet, or "None observed">
Positive signals:
- <signal, or "None observed">
Recommended redesign focus:
- <focus area, or "None identified">
Limitations:
- <limitation, or "None noted">
Factor assessments:
Hero imagery: <none|minor|moderate|severe|not_assessable> — <evidence; source=screenshots>
Readability: <none|minor|moderate|severe|not_assessable> — <evidence; source=screenshots>
Layout and mobile use: <none|minor|moderate|severe|not_assessable> — <evidence; source=screenshots>
Broken images: <none|minor|moderate|severe|not_assessable> — <evidence; source=script_audit>
Interactions: <none|minor|moderate|severe|not_assessable> — <evidence; source=script_audit>
Use at least one concise bullet in every section. Do not add a preamble. Keep the explicit score and verdict near the top. The Python workflow will normalize this labeled response into JSON. Do not claim a check succeeded unless the screenshots show that evidence."""
    prompt = ("Assess the supplied screenshot views and structured audit using the five fixed factors and rubric. The mobile_review_sheet.jpg contains separate labeled TOP and MID views. "
              "Match filenames to viewport/view names. The script audit is the evidence source for broken_images and interactions only; do not describe it as visually observed. "
              "Return the concise labeled text format requested in the instructions.\n\n"
              "STRUCTURED SCRIPT AUDIT (automated evidence; separate from visual scoring):\n"
              + json.dumps(context["script_audit"], ensure_ascii=False, indent=2))
    (output_dir / "candidate-context.json").write_text(json.dumps(context, indent=2) + "\n", encoding="utf-8")
    (output_dir / "instructions.txt").write_text(instructions + "\n", encoding="utf-8")
    (output_dir / "prompt.txt").write_text(prompt + "\n", encoding="utf-8")
    (output_dir / "input-manifest.json").write_text(json.dumps({"context": context, "images": images}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output_dir": str(output_dir), "candidate": context,
                      "images": images, "upload_images": upload_images}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
