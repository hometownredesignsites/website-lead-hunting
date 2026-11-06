#!/usr/bin/env python3
"""Prepare an auditable ChatGPT visual review from a saved candidate record."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
DESKTOP_MIN_WIDTH = 768
VIEWPORT_WIDTHS = {"desktop": 1440, "mobile": 390}


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
    lazy_loading = images.get("lazy_loading", {}) or {}
    image_request_failures = images.get("request_failures", []) or []
    # Chromium may cancel an image request during navigation, lazy-load
    # turnover, or screenshot capture. Keep these in diagnostics but do not
    # treat ERR_ABORTED by itself as a confirmed broken customer image.
    transient_image_failures = list(images.get("transient_request_failures", []) or [])
    confirmed_request_failures = []
    for failure in image_request_failures:
        reason = str((failure or {}).get("failure", "")).upper()
        if "ERR_ABORTED" in reason:
            transient_image_failures.append(failure)
        else:
            confirmed_request_failures.append(failure)
    image_request_failures = confirmed_request_failures
    failed_image_count = int(images.get("failed_image_count", 0) or 0)
    # Older records included all failed network requests in this count. When
    # their only evidence is ERR_ABORTED, remove that transient contribution.
    if transient_image_failures and not images.get("images"):
        failed_image_count = max(0, failed_image_count - len(transient_image_failures))
    image_status = images.get("status", "not_run")
    pending_images = images.get("pending_images", []) or []
    pending_non_lazy = any(str((item or {}).get("loading", "")).casefold() != "lazy"
                           for item in pending_images)
    # Apply the 20-image sample policy to older ledgers written before the
    # explicit `sampled` status existed. These sites can still be reviewed on
    # a fresh visual attempt without re-running discovery.
    sample_sufficient = bool(lazy_loading.get("sample_sufficient")) or int(lazy_loading.get("attempted", 0) or 0) >= 20
    if (image_status in {"partial", "sampled"} and sample_sufficient and not pending_non_lazy and
            not failed_image_count and not image_request_failures):
        image_status = "sampled"
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
            "status": image_status,
            "failed_image_count": failed_image_count,
            "lazy_loading": lazy_loading,
            "scope_note": images.get("scope_note") or (
                "At least 20 lazy-loaded images were inspected as a sufficient sample; additional lazy images do not make this factor uncertain."
                if image_status == "sampled" else ""),
            "images": _limited(images.get("images")),
            "request_failures": _limited(image_request_failures),
            "transient_request_failures": _limited(transient_image_failures),
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
                "available_count": links_interactions.get("available_count", 0),
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
        "image_enlargement": checks.get("image_enlargement", {"status": "not_run"}),
        "additional_page_reviews": checks.get("additional_page_reviews", {"pages_reviewed": 0, "pages": [], "status": "not_run"}),
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
        live_top = override_dir / f"{viewport}_top.png" if override_dir else None
        live_mid = override_dir / f"{viewport}_mid.png" if override_dir else None
        if live_top and live_mid and live_top.is_file() and live_mid.is_file():
            recapture_manifest_path = override_dir / "recapture-manifest.json"
            recapture_manifest = json.loads(recapture_manifest_path.read_text(encoding="utf-8"))
            capture = next((item for item in recapture_manifest.get("screenshots", [])
                            if item.get("viewport") == viewport), {})
            scroll_check = capture.get("scroll_validation") or {}
            actual_scroll = int(scroll_check.get("max_observed_scroll_px") or 0)
            for view, live_path in (("top", live_top), ("mid", live_mid)):
                with Image.open(live_path) as image:
                    validate_source_viewport(viewport, image.width, image.height, live_path)
                    if image.size != (VIEWPORT_WIDTHS[viewport], viewport_crop_heights[viewport]):
                        raise ValueError(
                            f"Fresh {viewport} {view} screenshot has unexpected dimensions: {image.size}"
                        )
                    target = output_dir / f"{viewport}_{view}.jpg"
                    image.convert("RGB").save(target, format="JPEG", quality=90, optimize=True)
                images.append({
                    "viewport": viewport,
                    "view": view,
                    "path": str(target),
                    "filename": target.name,
                    "source_path": str(live_path),
                    "bytes": target.stat().st_size,
                    "source_dimensions": {"width": VIEWPORT_WIDTHS[viewport],
                                          "height": viewport_crop_heights[viewport]},
                    "uploaded_dimensions": {"width": VIEWPORT_WIDTHS[viewport],
                                            "height": viewport_crop_heights[viewport]},
                    "source_top_px": 0 if view == "top" else actual_scroll,
                    "scrollable_height_px": actual_scroll,
                    "crop": "fresh browser viewport screenshot after real keyboard/wheel scroll",
                })
            continue
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
Use only the four supplied screenshots: desktop_top.jpg, desktop_mid.jpg, mobile_top.jpg, and mobile_mid.jpg. The top views show the start of each page; the mid views are viewport crops taken at 25% of the desktop and 48% of the mobile screenshot's scrollable height. These are representative page views, not guaranteed semantic sections. A structured script audit is also supplied. Treat it as automated evidence, not as something visually verified. Base all five visual factors (hero imagery, image quality, design freshness, readability, and layout/mobile use) on the screenshots. For layout/mobile use, page-flow measurements may be cited only as supplementary context; they do not change the required source tag, which must be source=screenshots, and the visual judgment must cite a screenshot. Use script_audit evidence only as the primary source for broken images and interactions. Keep evidence sources explicit and never describe an automated finding as visually observed. Do not browse the site or infer facts not shown in the screenshots or audit. Python is the only authority for score and site outcome. Return factor evidence and severities only; do not provide any score or overall verdict. Every factor must state whether it is assessed, not_applicable, or uncertain, with a specific reason/evidence and source. If evidence text or a limitation says a factor cannot be assessed, that factor must be uncertain rather than assessed.

Rate these seven fixed factors: hero_imagery (screenshots), image_quality (screenshots), design_freshness (screenshots), readability (screenshots), layout_mobile (screenshots), broken_images (script_audit), interactions (script_audit). For each, use state=assessed with severity none/minor/moderate/severe, state=not_applicable with no severity when the check truly does not apply, or state=uncertain with no severity when evidence is insufficient. Give concise, specific evidence or the reason for not_applicable/uncertain, and identify the evidence source. For broken_images, status=sampled means the audit inspected its sufficient sample of at least 20 lazy-loaded images; if failed_image_count is zero and request_failures is empty, assess this factor as none and state that the conclusion is based on the sample. Transient request failures are listed separately and do not establish broken customer-facing imagery by themselves. Do not mark it uncertain just because more lazy images exist beyond the sample. Report confirmed failures when listed. A global partial audit status may reflect another bounded check and does not override this per-factor sample rule. Do not create extra scoring factors or count issue sentences. Do not include a score, overall verdict, or inferred decision. Python calculates the score and outcome from named factors: none=0, minor=5, moderate=25, severe=50; not_applicable contributes zero; uncertain on any required factor produces an Uncertain outcome with no numeric score. When the script confirms no prominent image/video/background image in the first viewport, hero imagery receives at least 45 points; this replaces weaker points for that same issue. Moderate or severe overall dated-design findings also receive at least 45 points in design_freshness, replacing that factor's weaker points. Minor isolated outdated details stay at 5 points. The total is capped at 100. Verdict bands are Pass 0-49, Medium 50-74, High 75-100. Missing factor entries, state, source, or evidence invalidate the scorecard and trigger retry.

Use none when the factor is supported and no material weakness is present. Use minor for polish with little user impact; moderate for a clear redesign need or bounded user impact; severe for an obvious, prominent defect that substantially harms readability, trust, visual presentation, navigation, or ability to use the site. Evaluate the website as a potential redesign lead, not only as a technically functional page. For hero imagery, assess whether the first viewport has a compelling, relevant image/video that establishes the business and supports the headline or CTA. When script evidence confirms hero media is absent, identify that as script evidence and assess the text-only first view in the top screenshots; the deterministic 45-point floor applies. Use mid-view crops and page-flow measurements to report how far users scroll before meaningful project imagery appears. For image quality, look for visible blur, pixelation, stretching, poor crops, or consistently low-quality imagery; do not infer image quality from code or dimensions alone. Use minor for an isolated weak image, moderate when several prominent images are visibly poor, and severe when low-quality imagery dominates the hero or portfolio. For design freshness, judge the overall visual language using concrete screenshot evidence such as dated typography, effects, controls, spacing, iconography, or an overall style that materially looks old. Use minor for one limited outdated detail, moderate for dated patterns repeated across prominent sections, and severe when the pervasive presentation looks obsolete and materially weakens credibility. A clean, usable design need not follow current trends to rate none. A single stylistic preference is not evidence. Copyright dates are a separate weak content clue and must not determine this factor. For readability, check contrast and legibility, including text over images and text inside cards. For layout/mobile use, assess hierarchy, clutter, content organization, mobile clipping/overlap, and the presentation of the visible portfolio; keep datedness in design_freshness and do not score the same cause twice. Assess detached controls near navigation only when their position is visibly inconsistent with the layout.

For broken-image audit evidence, distinguish a confirmed site image failure from browser-policy or measurement artifacts. A cross-origin/ORB block, ERR_ABORTED, empty image URL, tracking pixel, or third-party social icon failure alone does not establish that customer-facing site imagery is broken. Use the URL, failure reason, alt text, and screenshot context; rate only material failures to imagery users need to view. If the script evidence cannot distinguish a site defect from a capture/browser limitation, mark the factor uncertain rather than treating every failed request as a broken site image. For interactions, a script audit status of not_applicable with zero available and tested controls means no safe menu/tab/disclosure control was found; mark state=not_applicable, with no severity and zero points. If safe controls were found but one or more were not tested, mark uncertain. If all discovered safe controls were tested, use state=assessed and rate their observed results. Never describe untested controls as working or broken.

For visual factors, cite the affected screenshot/view and concrete user impact. A hypothetical or hedged impact such as “may hinder” is not sufficient. Small or gray text is not a readability failure unless the screenshot shows that users would struggle to read it. A materially dated or amateur visual presentation, weak hero, sparse portfolio, or visibly poor image treatment may be a genuine redesign need even when navigation and basic controls work; describe the visible evidence and impact. Do not treat whitespace, repeated section styling, icon-only navigation, or other cosmetic preferences as material by themselves.

Do not infer missing or hidden content, poor usability, or a broken section from a dark or sparse-looking area alone. If the screenshot does not clearly show content obscured, unreadable text, an obstructed control, or another concrete user-facing problem, describe the ambiguous appearance as a limitation and do not count it toward the score.

If any prose or limitation says a factor could not be assessed, mark that factor uncertain. Do not contradict the limitation with an assessed/none rating. Do not state that the overall score or verdict is uncertain; express uncertainty on the relevant factor and let Python derive the overall outcome.

Keep issue descriptions for explanation; only the seven named factors and the deterministic absent-hero rule contribute to the durable score. The script's homepage image-enlargement check is a weak preliminary technical signal for portfolio-heavy sites: it reports explicit markup affordances or that none were detected, but cannot prove the absence of JavaScript lightbox behavior. It contributes only to the preliminary technical score and must not be treated as proof of a defect in the visual score.

Return a concise plain-text assessment, not JSON. Use this exact labeled format so the result can be converted reliably:
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
Hero imagery: <assessed (none|minor|moderate|severe) | not_applicable | uncertain> — <specific evidence/reason; source=screenshots>
Image quality: <assessed (none|minor|moderate|severe) | not_applicable | uncertain> — <specific evidence/reason; source=screenshots>
Design freshness: <assessed (none|minor|moderate|severe) | not_applicable | uncertain> — <specific evidence/reason; source=screenshots>
Readability: <assessed (none|minor|moderate|severe) | not_applicable | uncertain> — <specific evidence/reason; source=screenshots>
Layout and mobile use: <assessed (none|minor|moderate|severe) | not_applicable | uncertain> — <specific evidence/reason; source=screenshots>
Broken images: <assessed (none|minor|moderate|severe) | not_applicable | uncertain> — <specific evidence/reason; source=script_audit>
Interactions: <assessed (none|minor|moderate|severe) | not_applicable | uncertain> — <specific evidence/reason; source=script_audit>
Use at least one concise bullet in every descriptive section. Do not add a preamble. Do not report an overall score or verdict. The Python workflow will validate evidence, calculate points, and assign the site outcome. Do not claim a check succeeded unless the screenshots or audit show that evidence."""
    prompt = ("Assess the four supplied screenshots and structured audit using the seven fixed factors and rubric. "
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
