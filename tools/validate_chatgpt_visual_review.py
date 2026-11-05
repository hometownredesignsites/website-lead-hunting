#!/usr/bin/env python3
"""Validate a structured visual assessment and proof of both screenshot uploads."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from playwright_runner.prompt_contracts import extract_json_payload
from visual_factor_scoring import (ASSESSMENT_VERSION, enforce_script_audit_evidence,
                                   normalize_and_score, parse_factor_assessments)


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


def parse_labeled_text(response_text: str, context: dict) -> dict | None:
    score_match = re.search(
        r"^\s*Visual opportunity score\s*:\s*(?:(\d{1,3})\s*(?:/\s*100|out of 100)?|uncertain(?:\s*\(no score\))?)\s*$",
        response_text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    verdict_match = re.search(
        r"^\s*Verdict\s*:\s*(high|medium|pass|uncertain)\s*$",
        response_text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    confidence_match = re.search(
        r"^\s*Confidence\s*:\s*(low|medium|high)\s*$",
        response_text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if not score_match or not verdict_match or not confidence_match:
        return None

    verdict = verdict_match.group(1).lower()
    score = int(score_match.group(1)) if score_match.group(1) else None
    if (score is None) != (verdict == "uncertain"):
        return None

    observations = {key: [] for key in SECTION_KEYS.values()}
    current_section = None
    issue_cluster_sections_seen: set[str] = set()
    scalar_line = re.compile(r"^\s*(?:Visual opportunity score|Verdict|Confidence)\s*:", re.IGNORECASE)
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
        "visual_opportunity_score": score,
        "visual_verdict": verdict,
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
    attachment_records = metadata.get("image_attachments") or []
    uploaded_names = {item.get("filename") for item in attachment_records
                      if item.get("status") == "uploaded"}
    expected_names = {item.get("filename") for item in context.get("screenshots", [])}
    errors: list[str] = []
    if not review:
        errors.append("response has no parseable JSON object or labeled assessment scorecard")
    else:
        if review.get("candidate_id") != context.get("candidate_id"):
            errors.append("candidate_id does not match the source candidate")
        if review.get("website_url") != context.get("website_url"):
            errors.append("website_url does not match the source candidate")
        model_verdict = review.get("visual_verdict")
        model_score = review.get("visual_opportunity_score")
        if model_verdict not in {"high", "medium", "pass", "uncertain"}:
            errors.append("visual_verdict is not an allowed value")
        if model_verdict == "uncertain" and model_score is not None:
            errors.append("uncertain model verdict must use a null score")
        elif model_score is not None and (not isinstance(model_score, int) or isinstance(model_score, bool) or not 0 <= model_score <= 100):
            errors.append("model-reported visual score must be an integer from 0 to 100 or null")
        factors = parse_factor_assessments(response_text, review)
        audit = context.get("script_audit") or {}
        enforce_script_audit_evidence(factors, audit)
        scorecard = normalize_and_score(factors, audit)
        review["factor_assessments"] = scorecard["factors"]
        errors.extend(scorecard["errors"])
        review["model_reported_visual_opportunity_score"] = model_score
        review["model_reported_visual_verdict"] = model_verdict
        review["visual_opportunity_score"] = scorecard["score"]
        review["visual_verdict"] = scorecard["verdict"]
        review["score_calibration"] = {
            "version": ASSESSMENT_VERSION,
            "method": "sum factor points; confirmed first-viewport hero-media absence is at least 45 points (replaces weaker points for that same factor); none=0, minor=5, moderate=25, severe=50; capped at 100",
            "factor_points": {key: {"severity": scorecard["factors"][key]["severity"], "points": value} for key, value in scorecard.get("factor_points", {}).items()},
            "hero_media_status": scorecard.get("hero_media_status"),
            "hero_media_absence_points": scorecard.get("hero_media_absence_points", 0),
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
            "desktop_top_observations", "mobile_top_observations",
            "desktop_first_image_observations", "mobile_first_image_observations",
            "desktop_contact_form_observations", "mobile_contact_form_observations",
            "desktop_observations", "mobile_observations", "major_issues",
            "moderate_issue_clusters", "severe_issue_clusters",
            "positive_signals", "recommended_redesign_focus", "limitations",
        )
        for key in array_fields:
            if not isinstance(review.get(key), list) or not all(isinstance(item, str) for item in review.get(key, [])):
                errors.append(f"{key} must be an array of strings")
        for key in ("desktop_top_observations", "mobile_top_observations",
                    "desktop_first_image_observations", "mobile_first_image_observations",
                    "desktop_contact_form_observations", "mobile_contact_form_observations",
                    "desktop_observations", "mobile_observations"):
            if not review.get(key):
                errors.append(f"{key} must be non-empty")
    if uploaded_names != expected_names or len(attachment_records) != len(expected_names):
        errors.append("all expected desktop/mobile top, first-image, and contact-form screenshots were not confirmed uploaded")
    if len(expected_names) != 6:
        errors.append("candidate context must list exactly six desktop/mobile top, first-image, and contact-form screenshots")

    result = {
        "status": "passed" if not errors else "failed",
        "response_format": response_format,
        "candidate_id": context.get("candidate_id"),
        "expected_uploaded_images": sorted(expected_names),
        "confirmed_uploaded_images": sorted(uploaded_names),
        "review": review,
        "errors": errors,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
