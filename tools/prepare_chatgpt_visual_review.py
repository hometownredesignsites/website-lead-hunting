#!/usr/bin/env python3
"""Prepare an auditable ChatGPT visual review from a saved candidate record."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image
from visual_factor_scoring import classify_broken_image_audit


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
DESKTOP_MIN_WIDTH = 768
VIEWPORT_WIDTHS = {"desktop": 1440, "mobile": 390}


def content_aware_mid_position(flow: dict, max_scroll: int, crop_height: int) -> tuple[int, str]:
    """Place the mid crop around the first useful below-the-fold image when possible."""
    images = flow.get("content_images") or []
    usable = [item for item in images if isinstance(item, dict)
              and isinstance(item.get("document_y"), (int, float))
              and item.get("width", 0) >= 160 and item.get("height", 0) >= 100]
    # Prefer an image explicitly identified as project, portfolio, gallery, or
    # service work. If the page has no such labels, use the first substantial
    # image below the first viewport, then the first substantial image overall.
    terms = ("project", "portfolio", "gallery", "our work", "recent work", "before", "after",
             "remodel", "renovation", "service")
    candidate = next((item for item in usable
                      if any(term in " ".join(str(item.get(key) or "") for key in ("alt", "src")).casefold()
                             for term in terms)), None)
    if candidate is None:
        candidate = next((item for item in usable
                          if item["document_y"] >= crop_height * 0.8), None)
    if candidate is None and usable:
        candidate = usable[0]
    if candidate:
        image_y = int(candidate["document_y"])
        # Keep the image in the central part of the viewport crop with room
        # around it for the section heading and supporting content.
        top = max(0, min(max_scroll, image_y - int(crop_height * 0.28)))
        return top, (f"content-aware viewport crop centered on first meaningful {candidate.get('type', 'image')} "
                     f"at document y={image_y}px ({candidate.get('alt') or 'no alt text'})")
    return round(max_scroll * 0.25), "fallback viewport crop at 25% of scroll range; no meaningful image position available"


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
    image_classification = classify_broken_image_audit(images)
    image_request_failures = [
        item for item in (images.get("request_failures", []) or [])
        if "ERR_ABORTED" not in str((item or {}).get("failure", "")).upper()
    ]
    transient_image_failures = list(images.get("transient_request_failures", []) or [])
    image_request_failures = [item for item in image_request_failures
                              if item not in image_classification["unattributed_request_failures"]]
    failed_image_count = image_classification["confirmed_failed_image_count"]
    image_status = image_classification["status"]
    pending_images = images.get("pending_images", []) or []
    pending_non_lazy = any(str((item or {}).get("loading", "")).casefold() != "lazy"
                           for item in pending_images)
    # Apply the 20-image sample policy to older ledgers written before the
    # explicit `sampled` status existed. These sites can still be reviewed on
    # a fresh visual attempt without re-running discovery.
    sample_sufficient = bool(lazy_loading.get("sample_sufficient")) or int(lazy_loading.get("attempted", 0) or 0) >= 20
    every_lazy_image_inspected = bool(lazy_loading.get("complete"))
    if (image_status in {"partial", "sampled", "issues_found", "screenshot_fallback"} and
            (every_lazy_image_inspected or sample_sufficient) and not pending_non_lazy and
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
            "reported_failed_image_count": image_classification["reported_failed_image_count"],
            "unresolved_image_count": len(image_classification["unresolved_images"]),
            "screenshot_fallback_allowed": image_classification["screenshot_fallback_allowed"],
            "lazy_loading": lazy_loading,
            "scope_note": images.get("scope_note") or (
                "Automated image evidence was ambiguous; use only visible images in the four supplied screenshots as fallback, and do not infer that uncaptured page images were checked."
                if image_classification["screenshot_fallback_allowed"] and
                (image_classification["reported_failed_image_count"] or image_status not in {"clear", "sampled"}) else
                f"All {lazy_loading.get('attempted', 0)} discovered lazy-loaded images were inspected; no confirmed failures were found."
                if image_status == "sampled" and every_lazy_image_inspected else
                "At least 20 lazy-loaded images were inspected as a sufficient sample; additional lazy images do not make this factor uncertain."
                if image_status == "sampled" else ""),
            "images": _limited(images.get("images")),
            "request_failures": _limited(image_request_failures),
            "unattributed_request_failures": _limited(image_classification["unattributed_request_failures"]),
            "unresolved_images": _limited(image_classification["unresolved_images"]),
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
            actual_scroll = int(scroll_check.get(
                "capture_scroll_px", scroll_check.get("max_observed_scroll_px", 0)
            ) or 0)
            scrollable_range = int(scroll_check.get("scrollable_range_px") or actual_scroll)
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
                    "scrollable_height_px": scrollable_range,
                    "crop": "fresh browser viewport screenshot targeted at page content or scrolled with keyboard/wheel",
                    "capture_target": capture.get("mid_capture_target") if view == "mid" else None,
                })
            continue
        with Image.open(source) as image:
            original_size = image.size
            validate_source_viewport(viewport, image.width, image.height, source)
            crop_height = min(viewport_crop_heights[viewport], image.height)
            max_scroll = max(0, image.height - crop_height)
            flow = page_flow.get(viewport) or {}
            mid_top, mid_note = content_aware_mid_position(flow, max_scroll, crop_height)
            view_positions = {
                "top": (0, "top viewport crop from saved full-page screenshot"),
                "mid": (mid_top, mid_note),
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
                         "scrollable_height_px": item["scrollable_height_px"],
                         "capture_target": item.get("capture_target") or item.get("crop")}
                        for item in images],
    }
    instructions = """Assess this local-business website as a potential redesign lead. Use only desktop_top.jpg, desktop_mid.jpg, mobile_top.jpg, mobile_mid.jpg and the supplied structured script audit; do not browse or infer unseen facts. Top images show page starts. Mid images target substantial project/gallery/service imagery using page-flow evidence, then the first substantial below-fold image, then a scroll fallback. Treat audit data as automated evidence, never as visually verified.

Python alone calculates the durable score and verdict. Return evidence and factor severities only—no score, verdict, or inferred decision. Assess these seven factors only: hero_imagery, image_quality, design_freshness, readability, layout_mobile (source=screenshots); broken_images and interactions (source=script_audit, or screenshots_and_script_audit when image-audit fallback is used). Each factor needs state, source, and specific evidence/reason: assessed requires severity none/minor/moderate/severe; not_applicable and uncertain have no severity. Evidence or limitations saying a factor cannot be assessed require uncertain. Missing factor/state/source/evidence invalidates the scorecard.

SCORING (Python applies): none=0, minor=5, moderate=25, severe=50; not_applicable=0; any required uncertain factor means Uncertain/no score; cap=100; Pass=0–49, Medium=50–74, High=75–100. Confirmed absence of prominent first-view image/video/background sets a 45-point hero floor, replacing weaker hero points. Moderate/severe overall dated design sets a 45-point design_freshness floor; isolated outdated details are 5. A substantial first-view video is assumed to play: Python forces hero_imagery and image_quality to assessed/none (0), even if no frame decoded. Never call detected video absent or image_quality uncertain merely because still photos are not visible. These rules override model severities for those factors.

FACTOR RULES
- Hero imagery: judge whether relevant, compelling first-view media supports the business, headline, or CTA. If script confirms no hero media, cite that as audit evidence and describe the text-only top screenshot; Python applies the floor. Use mid screenshots/page-flow data to note how far users scroll before meaningful project imagery.
- Image quality: judge visible blur, pixelation, stretching, bad crops, or repeated poor imagery; never infer quality from code/dimensions. Minor=one weak image, moderate=several prominent weak images, severe=poor imagery dominates hero/portfolio.
- Design freshness: cite dated visual patterns (type, effects, controls, spacing, icons, overall style). Minor=one detail, moderate=patterns across prominent sections, severe=pervasive obsolete presentation harming credibility. Usable design need not follow trends; preference alone is not evidence. Copyright year is a separate weak clue, not this factor.
- Readability: judge visible contrast/legibility, including text over images and in cards. Small/gray text alone is not a defect unless users would struggle to read it.
- Layout/mobile: judge hierarchy, clutter, organization, clipping/overlap, and visible portfolio presentation. Keep datedness separate and do not count one cause twice. Score detached nav-area controls only if visibly out of place.
- Broken images: count only source-backed, visible customer-image failures or requests matched to an image element. Empty/missing/deferred src, hidden elements, uncorrelated ORB/cross-origin errors, ERR_ABORTED, tracking pixels, or third-party icons alone are not confirmed failures. If screenshot_fallback_allowed=true or the image audit did not run, assess the images visible in the four screenshots; state the screenshot coverage limit. If no broken image is visible, use assessed (none), source=screenshots_and_script_audit. Use uncertain only when the screenshots themselves do not let you assess visible images. Never claim uncaptured images were checked. If status=sampled with at least 20 inspected lazy images and zero confirmed failures, assess none based on the sample.
- Interactions: not_applicable plus zero available/tested controls means no safe menu/tab/disclosure was found (no severity, 0). If safe controls exist but any are untested, use uncertain. If all were tested, assess observed behavior; never claim untested controls work or fail.

For each visual factor cite the screenshot/view and concrete user impact; hypothetical “may hinder” claims are insufficient. A dated/amateur look, weak hero, sparse portfolio, or poor image treatment can merit redesign despite working controls. Do not score whitespace, repeated styling, icon-only navigation, or preference alone. Do not infer hidden content or defects from a dark/sparse area; note the limitation and do not score unless screenshots show obstruction, unreadable text, or another concrete problem. If prose says a factor could not be assessed, mark it uncertain; do not contradict this with assessed/none. Only named factors and the deterministic absent-hero rule score. The image-enlargement markup check is a weak preliminary signal and cannot prove JavaScript lightbox absence; do not count it as a visual defect.

Return concise plain text, not JSON, using exactly these labels:
Confidence: <low|medium|high>
Desktop top observations:
- <one observation tied to desktop_top.jpg>
Desktop mid observations:
- <one observation tied to desktop_mid.jpg and page-flow evidence>
Mobile top observations:
- <one observation tied to mobile_top.jpg>
Mobile mid observations:
- <one observation tied to mobile_mid.jpg and page-flow evidence>
Desktop observations:
- <summary of both desktop views>
Mobile observations:
- <summary of both mobile views>
Major issues:
- <issue or "None observed">
Moderate issue clusters:
- <one independently evidenced moderate-impact issue per bullet, or "None observed">
Severe issue clusters:
- <one independently evidenced severe-impact issue per bullet, or "None observed">
Positive signals:
- <signal or "None observed">
Recommended redesign focus:
- <focus or "None identified">
Limitations:
- <limitation or "None noted">
Factor assessments:
Hero imagery: <assessed (none|minor|moderate|severe) | not_applicable | uncertain> — <evidence/reason; source=screenshots>
Image quality: <assessed (none|minor|moderate|severe) | not_applicable | uncertain> — <evidence/reason; source=screenshots>
Design freshness: <assessed (none|minor|moderate|severe) | not_applicable | uncertain> — <evidence/reason; source=screenshots>
Readability: <assessed (none|minor|moderate|severe) | not_applicable | uncertain> — <evidence/reason; source=screenshots>
Layout and mobile use: <assessed (none|minor|moderate|severe) | not_applicable | uncertain> — <evidence/reason; source=screenshots>
Broken images: <assessed (none|minor|moderate|severe) | not_applicable | uncertain> — <evidence/reason; source=script_audit>
Interactions: <assessed (none|minor|moderate|severe) | not_applicable | uncertain> — <evidence/reason; source=script_audit>
Include a concise bullet in every descriptive section. No preamble, score, or verdict. Do not claim checks succeeded without evidence; Python validates, scores, and assigns the outcome."""

    prompt = ("Assess the four attached screenshots under the seven-factor rubric in the instructions. "
              "Match each filename to its viewport and view. Use structured context.script_audit only for "
              "broken_images and interactions; keep other factors screenshot-based. Return the required "
              "labeled text only, with no score or verdict.")
    (output_dir / "candidate-context.json").write_text(json.dumps(context, indent=2) + "\n", encoding="utf-8")
    (output_dir / "instructions.txt").write_text(instructions + "\n", encoding="utf-8")
    (output_dir / "prompt.txt").write_text(prompt + "\n", encoding="utf-8")
    (output_dir / "input-manifest.json").write_text(json.dumps({"context": context, "images": images}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output_dir": str(output_dir), "candidate": context, "images": images}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
