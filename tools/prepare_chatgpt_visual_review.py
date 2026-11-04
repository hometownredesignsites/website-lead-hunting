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
    instructions = """You are reviewing a local business website as a visual redesign-opportunity assessor.
Use the four supplied screenshots for all visual judgments: desktop_top.jpg, desktop_mid.jpg, mobile_top.jpg, and mobile_mid.jpg. The top views show the start of each page; the mid views are viewport crops taken at 35% of the desktop and 60% of the mobile screenshot's scrollable height. These are representative page views, not guaranteed semantic sections. A separate structured script audit is supplied in the prompt. Treat it as automated evidence, not as something you visually verified. Keep script findings out of the visual score, visual issue clusters, and screenshot observations. Do not browse the site or treat the supplied title/URL as visual evidence. Do not infer facts that neither the screenshots nor the audit establish (such as current business status, licensing, or technology). If an image is missing, obstructed, too small, or not interpretable, lower confidence and state the limitation; use an uncertain verdict and null score only when the combined visual evidence is insufficient.

Score redesign opportunity with an integer from 0 to 100: higher means stronger visual evidence that a redesign could materially improve the site. Assess the material impact of the visible issues, not how many refinements you can suggest. A polished, credible site with strong relevant imagery, clear services/contact, and usable desktop/mobile layouts is pass when its remaining observations are preferences or minor polish.

Use pass (0-49) when no clearly evidenced material weakness is likely to harm trust, comprehension, readability, navigation, mobile use, or the ability to request service. Small type, generous spacing, repeated components, restrained styling, or icon-based navigation are not by themselves meaningful redesign opportunities. You may mention these as minor refinements, but do not let them raise the score into medium.

Use medium (50-74) only when the screenshots show either one severe, clearly demonstrated weakness or at least two independent moderate weaknesses that together materially reduce usability, trust, comprehension, or conversion. For each weakness, name the specific affected viewport and the observable user impact (for example, text that cannot be read, a service/contact action that cannot be found, or a layout that prevents practical mobile use). A hypothetical or hedged impact such as “may hinder” is not sufficient. Small or gray text is not a readability failure unless the screenshot shows that users would struggle to read it. Do not treat whitespace, repeated section styling, icon-only navigation, or other cosmetic preferences as material by themselves. If the only supported findings are minor refinements or speculative impacts, the score must be 0-49 and the verdict must be pass.

Do not infer missing or hidden content, poor usability, or a broken section from a dark or sparse-looking area alone. If the screenshot does not clearly show content obscured, unreadable text, an obstructed control, or another concrete user-facing problem, describe the ambiguous appearance as a limitation and do not count it toward the score.

Use high (75-100) only when the evidence-cluster calculation reaches 75. Because the calculation advances in ten-point steps from 30, the first reachable high score is 80; examples are five moderate clusters, one severe plus three moderate clusters, or two severe plus one moderate cluster. Two severe clusters alone score 70 (medium). Use uncertain with a null score when the combined evidence is insufficient. Score bands: high=75-100, medium=50-74, pass=0-49.

List each independently evidenced moderate or severe issue cluster in its own section below. Do not count cosmetic refinements, repeat observations of the same root problem, speculative effects, or the same text-size complaint across several screenshots more than once. A moderate cluster must name a visible user-facing consequence. A severe cluster must show an obvious obstruction or substantial loss of readability, navigation, trust, or ability to request service. A dark panel, compact appearance, or small-looking text does not qualify by itself. If legibility is uncertain at the supplied image resolution, call it out as a limitation and do not count it as a material issue. The Python validator will calculate the durable score as min(100, 30 + 10*M + 20*S), where M and S are the number of listed moderate and severe clusters. Thus no issues=30, one moderate=40, two moderate=50, one severe=50. Verdict is derived from this score; report your own score and verdict too so the raw model judgment is retained separately.

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
Use at least one concise bullet in every section. Do not add a preamble. Keep the explicit score and verdict near the top. The Python workflow will normalize this labeled response into JSON. Do not claim a check succeeded unless the screenshots show that evidence."""
    prompt = ("Assess all four supplied screenshots and score the visual redesign opportunity under the rubric. "
              "Match filenames to the viewport and view names in the supplied context. The script audit below is a separate evidence source: do not use it to raise or lower the screenshot-only visual score, and do not describe it as visually observed. "
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
