"""Shared factor-based visual opportunity scoring contract."""
from __future__ import annotations

import re


FACTORS = {
    "hero_imagery": "Hero imagery",
    "readability": "Readability",
    "layout_mobile": "Layout and mobile use",
    "broken_images": "Broken images",
    "interactions": "Interactions",
}
SEVERITY_POINTS = {"none": 0, "minor": 5, "moderate": 25, "severe": 50}
FACTOR_SOURCES = {
    "hero_imagery": "screenshots", "readability": "screenshots",
    "layout_mobile": "screenshots", "broken_images": "script_audit",
    "interactions": "script_audit",
}


def parse_factor_assessments(response_text: str, review: dict) -> dict:
    """Parse fixed factor lines from the reliable labeled-text response format."""
    factors = review.get("factor_assessments") or review.get("factors")
    if isinstance(factors, dict):
        return factors
    result = {}
    for key, label in FACTORS.items():
        match = re.search(
            rf"^\s*{re.escape(label)}\s*:\s*(none|minor|moderate|severe|not[_ -]assessable)\s*(?:—|--|-)\s*(.*?)\s*$",
            response_text,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        if match:
            severity = match.group(1).casefold().replace(" ", "_").replace("-", "_")
            evidence = re.sub(r"\s*;?\s*source=(?:screenshots|script_audit|screenshots_and_script_audit)\s*$", "", match.group(2).strip(), flags=re.I)
            result[key] = {"severity": severity, "evidence": evidence, "source": FACTOR_SOURCES[key]}
    return result


def enforce_script_audit_evidence(factors: dict, audit: dict) -> dict:
    """Prevent unavailable automated checks from being rated as clean."""
    image_status = (audit.get("broken_images") or {}).get("status", "not_run")
    if image_status not in {"clear", "issues_found"}:
        factors["broken_images"] = {
            "severity": "not_assessable",
            "evidence": f"Automated broken-image audit status is {image_status}; findings are incomplete.",
            "source": "script_audit",
        }
    interactions_status = (audit.get("broken_links_and_interactions") or {}).get("status", "not_run")
    if interactions_status not in {"clear", "issues_found"}:
        factors["interactions"] = {
            "severity": "not_assessable",
            "evidence": f"Automated links/interactions audit status is {interactions_status}; findings are unavailable.",
            "source": "script_audit",
        }
    return factors


def normalize_and_score(factors: dict) -> dict:
    """Validate required evidence and compute a score from factor severity only."""
    normalized = {}
    errors = []
    uncertain = []
    for key, label in FACTORS.items():
        item = factors.get(key)
        if not isinstance(item, dict):
            errors.append(f"required factor assessment missing: {key}")
            continue
        severity = str(item.get("severity", "")).strip().casefold().replace(" ", "_").replace("-", "_")
        evidence = item.get("evidence")
        source = item.get("source")
        if severity not in {*SEVERITY_POINTS, "not_assessable"}:
            errors.append(f"{key} severity must be none, minor, moderate, severe, or not_assessable")
            continue
        if not isinstance(evidence, str) or not evidence.strip():
            errors.append(f"{key} requires specific supporting evidence or a reason it cannot be assessed")
            continue
        if source and source != FACTOR_SOURCES[key]:
            errors.append(f"{key} evidence source must be {FACTOR_SOURCES[key]}")
            continue
        if severity == "not_assessable":
            uncertain.append(key)
        normalized[key] = {
            "label": label,
            "severity": severity,
            "evidence": evidence.strip(),
            "source": FACTOR_SOURCES[key],
        }
    if errors:
        return {"errors": errors, "factors": normalized, "score": None, "verdict": "uncertain", "uncertain_factors": uncertain}
    if uncertain:
        return {"errors": [], "factors": normalized, "score": None, "verdict": "uncertain", "uncertain_factors": uncertain}
    score = min(100, sum(SEVERITY_POINTS[item["severity"]] for item in normalized.values()))
    verdict = "pass" if score < 50 else "medium" if score < 75 else "high"
    return {"errors": [], "factors": normalized, "score": score, "verdict": verdict, "uncertain_factors": []}
