#!/usr/bin/env python3
"""Prepare an auditable ChatGPT visual review from a saved candidate record."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image


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
    page_flow = candidate.get("visual_page_flow") or {
        viewport: ((evidence.get(viewport) or {}).get("pageFlow") or
                   ((candidate.get("screenshots") or {}).get(viewport) or {}).get("page_flow") or {})
        for viewport in ("desktop", "mobile")
    }

    http = checks.get("http_links_and_resources") or {}
    images = checks.get("broken_images") or {}
    forms = checks.get("contact_form_privacy") or {}
    links = checks.get("broken_links_and_interactions") or {}
    links_interactions = links.get("interactions") or {}
    email = checks.get("contact_email_opportunity") or {}

    summary = {
        "status": checks.get("status", "not_run"),
        "automated_priority": candidate.get("automated_priority"),
        "website_opportunity_score": candidate.get("website_opportunity_score"),
        "evidence_confidence": candidate.get("evidence_confidence"),
        "hero_media": hero_media,
        "visual_page_flow": page_flow,
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
                "status": links_interactions.get("status", "not_run"),
                "tested_count": links_interactions.get("tested_count", 0),
                "findings": _limited(links_interactions.get("findings")),
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--screenshot-override-dir", type=Path,
                        help="Optional directory with freshly captured desktop.png and mobile.png files")
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    output_dir = args.output_dir.resolve()
    candidate = load_candidate(run_dir, args.candidate_id)
    screenshot_record = candidate.get("screenshots") or {}
    override_dir = args.screenshot_override_dir.resolve() if args.screenshot_override_dir else None
    page_flow = script_audit_summary(candidate).get("visual_page_flow") or {}
    output_dir.mkdir(parents=True, exist_ok=True)
    images: list[dict] = []
    viewport_crop_heights = {"desktop": 900, "mobile": 844}
    mid_page_scroll_fractions = {"desktop": 0.25, "mobile": 0.48}
    for viewport in ("desktop", "mobile"):
        record = screenshot_record.get(viewport) or {}
        if override_dir is None and (record.get("status") != "saved" or not record.get("path")):
            raise ValueError(f"Candidate {args.candidate_id} has no saved {viewport} screenshot")
        source = (override_dir / f"{viewport}.png" if override_dir else
                  resolve_screenshot(record["path"], run_dir))
        if source is None:
            raise FileNotFoundError(
                f"Saved {viewport} screenshot is unavailable in the downloaded lead-hunt artifact"
            )
        with Image.open(source) as image:
            original_size = image.size
            validate_source_viewport(viewport, image.width, image.height, source)
            crop_height = min(viewport_crop_heights[viewport], image.height)
            max_scroll = max(0, image.height - crop_height)
            flow = page_flow.get(viewport) or {}
            mid_fraction = mid_page_scroll_fractions[viewport]
            view_positions = {
                "top": (0, "top viewport crop from saved full-page screenshot"),
                "mid": (round(max_scroll * mid_fraction),
                       f"viewport crop at {mid_fraction:.0%} of the scrollable page"),
            }
            for view, (top, crop_note) in view_positions.items():
                target = output_dir / f"{viewport}_{view}.jpg"
                image.crop((0, top, image.width, top + crop_height)).convert("RGB").save(
                    target, format="JPEG", quality=90, optimize=True
                )
                images.append({
                    "viewport": viewport,
                    "view": view,
                    "path": str(target),
                    "filename": target.name,
                    "source_path": (str(source) if override_dir else record["path"]),
                    "bytes": target.stat().st_size,
                    "source_dimensions": {"width": original_size[0], "height": original_size[1]},
                    "uploaded_dimensions": {"width": image.width, "height": crop_height},
                    "source_top_px": top,
                    "scrollable_height_px": max_scroll,
                    "crop": crop_note,
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
    instructions = """You are reviewing a local business website as a visual redesign-opportunity assessor.
Use only the four supplied screenshots: desktop_top.jpg, desktop_mid.jpg, mobile_top.jpg, and mobile_mid.jpg. The top views show the start of each page; the mid views are viewport crops taken at 25% of the desktop and 48% of the mobile screenshot's scrollable height. These are representative page views, not guaranteed semantic sections. A structured script audit is also supplied. Treat it as automated evidence, not as something visually verified. Use screenshots and page-flow measurements for hero imagery, readability, and layout/mobile use. Use script audit evidence only for broken images and interactions. Keep evidence sources explicit and never describe an automated finding as visually observed. Do not browse the site or infer facts not shown in the screenshots or audit. If a required factor cannot be assessed from its evidence source, mark it not_assessable; the durable result will be uncertain instead of silently lowering the score. A separate model-level uncertain verdict does not make otherwise complete factor evidence unassessable; rate each factor from its evidence and let the validator calculate the final result.

Rate these five fixed factors, using exactly one severity per factor: none, minor, moderate, severe, or not_assessable. Give concise specific evidence and identify the evidence source. Factors: hero_imagery (screenshots), readability (screenshots), layout_mobile (screenshots), broken_images (script_audit), interactions (script_audit). Do not create extra scoring factors or count issue sentences. Python calculates points from named factors: none=0, minor=5, moderate=25, severe=50. When the script confirms no prominent image/video/background image in the first viewport, hero imagery receives at least 45 points; this replaces weaker points for that same issue. Independently evidenced issues in other factors add points. The total is capped at 100. Verdict bands are pass 0-49, medium 50-74, high 75-100. `not_assessable` in any required factor makes the durable verdict uncertain with a null score. Missing factor entries or evidence make the scorecard invalid and trigger retry. Report your own score and verdict separately; they are retained only as model output.

Use none when the factor is supported and no material weakness is present. Use minor for polish with little user impact; moderate for a clear redesign need or bounded user impact; severe for an obvious, prominent defect that substantially harms readability, trust, visual presentation, navigation, or ability to use the site. Evaluate the website as a potential redesign lead, not only as a technically functional page. For hero imagery, assess whether the first viewport has a compelling, relevant image/video that establishes the business and supports the headline or CTA. When script evidence confirms hero media is absent, identify that as script evidence and assess the text-only first view in the top screenshots; the deterministic 45-point floor applies. Use mid-view crops and page-flow measurements to report how far users scroll before meaningful project imagery appears. For readability, check contrast and legibility, including text over images and text inside cards. For layout/mobile use, assess hierarchy, clutter, consistency, whether the design looks materially dated or amateurish, mobile clipping/overlap, and the presentation of the visible portfolio. Assess detached controls near navigation only when their position is visibly inconsistent with the layout. Do not count the same root cause twice; do not treat style preference as evidence by itself or infer image quality from code.

For broken-image audit evidence, distinguish a confirmed site image failure from browser-policy or measurement artifacts. A cross-origin/ORB block, ERR_ABORTED, empty image URL, tracking pixel, or third-party social icon failure alone does not establish that customer-facing site imagery is broken. Use the URL, failure reason, alt text, and screenshot context; rate only material failures to imagery users need to view. If the script evidence cannot distinguish a site defect from a capture/browser limitation, mark the factor not_assessable rather than treating every failed request as a broken site image. For interactions, a script audit status of not_applicable with zero tested controls means no safe menu/tab/disclosure control was found to test; do not claim a control was tested or broken.

For visual factors, cite the affected screenshot/view and concrete user impact. A hypothetical or hedged impact such as “may hinder” is not sufficient. Small or gray text is not a readability failure unless the screenshot shows that users would struggle to read it. A materially dated or amateur visual presentation, weak hero, sparse portfolio, or visibly poor image treatment may be a genuine redesign need even when navigation and basic controls work; describe the visible evidence and impact. Do not treat whitespace, repeated section styling, icon-only navigation, or other cosmetic preferences as material by themselves.

Do not infer missing or hidden content, poor usability, or a broken section from a dark or sparse-looking area alone. If the screenshot does not clearly show content obscured, unreadable text, an obstructed control, or another concrete user-facing problem, describe the ambiguous appearance as a limitation and do not count it toward the score.

Use uncertain with a null score when a required factor cannot be assessed; otherwise let the named factor points and absent-hero rule determine the durable score and verdict.

Keep issue descriptions for explanation; only the five named factors and the deterministic absent-hero rule contribute to the durable score.

Return a concise plain-text assessment, not JSON. Use this exact labeled format so the result can be converted reliably:
Visual opportunity score: <integer 0-100>/100, or "uncertain (no score)" when the visual evidence is insufficient
Verdict: <high|medium|pass|uncertain>
Confidence: <low|medium|high>
Desktop top observations:
- <one concise observation tied to desktop_top.jpg>
Desktop mid observations:
- <one concise observation tied to desktop_mid.jpg and page-flow evidence>
Mobile top observations:
- <one concise observation tied to mobile_top.jpg>
Mobile mid observations:
- <one concise observation tied to mobile_mid.jpg and page-flow evidence>
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
    prompt = ("Assess the four supplied screenshots and structured audit using the five fixed factors and rubric. "
              "Match filenames to viewport/view names. The script audit is the evidence source for broken_images and interactions only; do not describe it as visually observed. "
              "Return the concise labeled text format requested in the instructions.\n\n"
              "STRUCTURED SCRIPT AUDIT (automated evidence; separate from visual scoring):\n"
              + json.dumps(context["script_audit"], ensure_ascii=False, indent=2))
    (output_dir / "candidate-context.json").write_text(json.dumps(context, indent=2) + "\n", encoding="utf-8")
    (output_dir / "instructions.txt").write_text(instructions + "\n", encoding="utf-8")
    (output_dir / "prompt.txt").write_text(prompt + "\n", encoding="utf-8")
    (output_dir / "input-manifest.json").write_text(json.dumps({"context": context, "images": images}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output_dir": str(output_dir), "candidate": context, "images": images}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
