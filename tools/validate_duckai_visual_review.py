#!/usr/bin/env python3
"""Validate a structured visual assessment and proof of both screenshot uploads."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from playwright_runner.prompt_contracts import extract_json_payload
from visual_factor_scoring import (ASSESSMENT_VERSION, FACTORS, enforce_script_audit_evidence,
                                   has_prominent_first_view_video, normalize_and_score,
                                   parse_factor_assessments)


SECTION_KEYS = {
    "desktop top observations": "desktop_top_observations",
    "desktop mid observations": "desktop_mid_observations",
    "mobile top observations": "mobile_top_observations",
    "mobile mid observations": "mobile_mid_observations",
    "desktop first-image observations": "desktop_first_image_observations",
    "mobile first-image observations": "mobile_first_image_observations",
    "desktop contact-form observations": "desktop_contact_form_observations",
    "mobile contact-form observations": "mobile_contact_form_observations",
    "desktop observations": "desktop_observations",
    "mobile observations": "mobile_observations",
    "major issues": "major_issues",
    "moderate issue clusters": "moderate_issue_clusters",
    "severe issue clusters": "severe_issue_clusters",
    "positive signals": "positive_signals",
    "recommended redesign focus": "recommended_redesign_focus",
    "limitations": "limitations",
}


def validate_image_uploads(metadata: dict, context: dict) -> dict:
    """Verify Duck.ai's three uploaded images cover all four source views."""
    attachment_records = metadata.get("image_attachments") or []
    uploaded_names = {item.get("filename") for item in attachment_records
                      if item.get("status") == "uploaded"}
    upload_images = context.get("upload_images", [])
    expected_names = {item.get("filename") for item in upload_images}
    source_names = {item.get("filename") for item in context.get("screenshots", [])}
    covered = [name for item in upload_images for name in item.get("covers", [])]
    covered_names = set(covered)
    errors = []
    if uploaded_names != expected_names or len(attachment_records) != len(expected_names) or len(expected_names) != 3:
        errors.append("all three expected image attachments were not confirmed uploaded")
    if len(source_names) != 4:
        errors.append("candidate context must list exactly four source top/mid desktop/mobile screenshots")
    if covered_names != source_names or len(covered) != 4:
        errors.append("uploaded images do not cover all four source top/mid screenshots exactly")
    return {"errors": errors, "expected_uploaded_images": sorted(expected_names),
            "confirmed_uploaded_images": sorted(uploaded_names),
            "covered_source_screenshots": sorted(covered_names)}


def parse_labeled_text(response_text: str, context: dict) -> dict | None:
    confidence_match = re.search(
        r"^\s*Confidence\s*:\s*(low|medium|high)\s*$",
        response_text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if not confidence_match:
        return None

    observations = {key: [] for key in SECTION_KEYS.values()}
    current_section = None
    issue_cluster_sections_seen: set[str] = set()
    scalar_line = re.compile(r"^\s*Confidence\s*:", re.IGNORECASE)
    bullet_prefix = re.compile(r"^\s*(?:[-*•]+|\d+[.)])\s*")
    for line in response_text.splitlines():
        if scalar_line.match(line):
            current_section = None
            continue
        heading = re.match(r"^\s*([A-Za-z][A-Za-z ]*?)\s*:\s*(.*)$", line)
        if heading:
            current_section = SECTION_KEYS.get(heading.group(1).strip().lower())
            if current_section in {"moderate_issue_clusters", "severe_issue_clusters"}:
                issue_cluster_sections_seen.add(current_section)
            if current_section and heading.group(2).strip():
                item = bullet_prefix.sub("", heading.group(2)).strip()
                if item and item.lower() not in {"none observed", "none identified", "none noted", "none", "n/a"}:
                    observations[current_section].append(item)
            continue
        if current_section and line.strip():
            item = bullet_prefix.sub("", line).strip()
            if item and item.lower() not in {"none observed", "none identified", "none noted"}:
                observations[current_section].append(item)

    return {
        "candidate_id": context.get("candidate_id"),
        "website_url": context.get("website_url"),
        "confidence": confidence_match.group(1).lower(),
        "issue_clusters_reported": issue_cluster_sections_seen == {
            "moderate_issue_clusters", "severe_issue_clusters"
        },
        "factor_assessments": parse_factor_assessments(response_text, {}),
        **observations,
    }


def cluster_count(review: dict, key: str) -> int:
    items = review.get(key)
    if not isinstance(items, list):
        return 0
    empty_cluster = re.compile(
        r"^(?:none(?:\s+(?:observed|identified|noted))?|n\s*/\s*a|no\s+issues?)(?:[.!\s]*)$",
        re.IGNORECASE,
    )
    normalized = {
        " ".join(item.casefold().split())
        for item in items
        if isinstance(item, str)
        and item.strip()
        and not empty_cluster.fullmatch(item.strip())
    }
    return len(normalized)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    response_text = args.response.read_text(encoding="utf-8")
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    context = json.loads(args.context.read_text(encoding="utf-8"))
    review = extract_json_payload(response_text)
    response_format = "json"
    if review is None:
        review = parse_labeled_text(response_text, context)
        response_format = "labeled_text" if review is not None else "unparsed"
    image_validation = validate_image_uploads(metadata, context)
    expected_names = set(image_validation["expected_uploaded_images"])
    uploaded_names = set(image_validation["confirmed_uploaded_images"])
    errors: list[str] = []
    errors.extend(image_validation["errors"])
    if not review:
        errors.append("response has no parseable JSON object or labeled assessment scorecard")
    else:
        if review.get("candidate_id") != context.get("candidate_id"):
            errors.append("candidate_id does not match the source candidate")
        if review.get("website_url") != context.get("website_url"):
            errors.append("website_url does not match the source candidate")
        if "visual_opportunity_score" in review or "visual_verdict" in review:
            errors.append("model response must not provide a final score or site verdict; Python is authoritative")
        if re.search(r"^\s*(?:Visual opportunity score|Verdict)\s*:", response_text,
                     flags=re.IGNORECASE | re.MULTILINE):
            errors.append("model response contains a final score/verdict heading; return factor evidence only")
        if re.search(r"\b(?:durable|final|overall|site)\s+(?:score|verdict|result|assessment)\b.{0,120}\buncertain\b",
                     response_text, flags=re.IGNORECASE | re.DOTALL):
            errors.append("model response declares the final result uncertain; express uncertainty on the relevant factor instead")
        factors = parse_factor_assessments(response_text, review)
        audit = context.get("script_audit") or {}
        hero_video_present = has_prominent_first_view_video(audit)
        enforce_script_audit_evidence(factors, audit)
        scorecard = normalize_and_score(factors, audit)
        review["factor_assessments"] = scorecard["factors"]
        errors.extend(scorecard["errors"])
        for key, factor in scorecard["factors"].items():
            if factor.get("state") == "uncertain":
                continue
            # The deterministic video rule supersedes uncertainty about still
            # photos: an in-view hero video is assumed to play and show work.
            if key in {"hero_imagery", "image_quality"} and hero_video_present:
                continue
            label = re.escape(FACTORS[key])
            if key == "interactions":
                label = r"interaction(?:s)?"
            uncertainty_near_factor = re.search(
                rf"(?:{label})[^.\n]{{0,100}}(?:not assessable|cannot be assessed|unable to assess|insufficient evidence|not assessed)"
                rf"|(?:not assessable|cannot be assessed|unable to assess|insufficient evidence|not assessed)[^.\n]{{0,100}}(?:{label})",
                response_text, flags=re.IGNORECASE,
            )
            if uncertainty_near_factor:
                errors.append(f"{key} evidence contradicts prose that says the factor is not assessable")
        review["visual_opportunity_score"] = scorecard["score"]
        review["visual_verdict"] = scorecard["verdict"]
        review["site_outcome"] = scorecard["verdict"]
        review["review_execution_status"] = "completed"
        review["score_calibration"] = {
            "version": ASSESSMENT_VERSION,
            "method": "Python-only sum of assessed factor points; not_applicable factors contribute zero; uncertain factors produce a null score and uncertain site outcome; prominent first-viewport video forces hero-imagery and image-quality points to zero; confirmed first-viewport hero-media absence and moderate/severe dated design each receive at least 45 points within their own factor; displayed copyright year 1-2 years old adds 5 points, more than 2 years old adds 10; none=0, minor=5, moderate=25, severe=50; capped at 100",
            "factor_points": {key: {"state": scorecard["factors"][key]["state"], "severity": scorecard["factors"][key]["severity"], "points": value} for key, value in scorecard.get("factor_points", {}).items()},
            "hero_media_status": scorecard.get("hero_media_status"),
            "hero_media_absence_points": scorecard.get("hero_media_absence_points", 0),
            "hero_video_present": scorecard.get("hero_video_present", hero_video_present),
            "dated_design_floor_points": scorecard.get("dated_design_floor_points", 0),
            "stale_copyright_points": scorecard.get("stale_copyright_points", 0),
            "uncertain_factors": scorecard["uncertain_factors"],
        }
        review["raw_model_response"] = response_text
        verdict, score = scorecard["verdict"], scorecard["score"]
        if verdict == "uncertain":
            if score is not None:
                errors.append("uncertain verdict must use a null score")
        elif not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 100:
            errors.append("calibrated visual opportunity score must be an integer from 0 to 100")
        elif ((verdict == "high" and score < 75)
              or (verdict == "medium" and not 50 <= score <= 74)
              or (verdict == "pass" and score > 49)):
            errors.append("calibrated verdict does not match its score band")
        if review.get("confidence") not in {"low", "medium", "high"}:
            errors.append("confidence is not an allowed value")
        review["script_audit_evidence"] = context.get("script_audit", {"status": "not_available"})
        array_fields = (
            "desktop_top_observations", "desktop_mid_observations",
            "mobile_top_observations", "mobile_mid_observations",
            "desktop_observations", "mobile_observations", "major_issues",
            "moderate_issue_clusters", "severe_issue_clusters",
            "positive_signals", "recommended_redesign_focus", "limitations",
        )
        for key in array_fields:
            if not isinstance(review.get(key), list) or not all(isinstance(item, str) for item in review.get(key, [])):
                errors.append(f"{key} must be an array of strings")
        for key in ("desktop_top_observations", "desktop_mid_observations",
                    "mobile_top_observations", "mobile_mid_observations",
                    "desktop_observations", "mobile_observations"):
            if not review.get(key):
                errors.append(f"{key} must be non-empty")
    if isinstance(review, dict) and errors:
        review["review_execution_status"] = "completed"
        review["scorecard_validation_status"] = "invalid"
        review["site_outcome"] = "uncertain"
        review["visual_opportunity_score"] = None
        review["visual_verdict"] = "uncertain"
    elif isinstance(review, dict):
        review["review_execution_status"] = "completed"
        review["scorecard_validation_status"] = "valid"

    result = {
        "status": "passed" if not errors else "failed",
        "review_execution_status": "completed" if isinstance(review, dict) else "incomplete",
        "scorecard_validation_status": "valid" if not errors else "invalid",
        "site_outcome": ((review or {}).get("site_outcome") if not errors else "uncertain"),
        "response_format": response_format,
        "candidate_id": context.get("candidate_id"),
        "expected_uploaded_images": sorted(expected_names),
        "confirmed_uploaded_images": sorted(uploaded_names),
        "covered_source_screenshots": image_validation["covered_source_screenshots"],
        "review": review,
        "errors": errors,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
