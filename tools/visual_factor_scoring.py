"""Shared factor-based visual opportunity scoring contract."""
from __future__ import annotations

import re


ASSESSMENT_VERSION = "fixed-factors-v3"

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
    """Parse fixed factor assessments from labeled text or JSON review data."""
    factors = review.get("factor_assessments") or review.get("factors")
    result = dict(factors) if isinstance(factors, dict) else {}
    label_to_key = {label.casefold(): key for key, label in FACTORS.items()}
    labels = "|".join(re.escape(label) for label in FACTORS.values())
    pattern = re.compile(
        rf"(?P<label>{labels})\s*:\s*(?P<severity>none|minor|moderate|severe|not[_ -]assessable)\s*(?:—|--|-|�)\s*",
        flags=re.IGNORECASE,
    )
    matches = list(pattern.finditer(response_text))
    for index, match in enumerate(matches):
        key = label_to_key[match.group("label").casefold()]
        evidence_end = matches[index + 1].start() if index + 1 < len(matches) else len(response_text)
        evidence = response_text[match.end():evidence_end].strip()
        evidence = re.sub(
            r"\s*;?\s*source=(?:screenshots|script_audit|screenshots_and_script_audit)\s*$",
            "", evidence, flags=re.I,
        ).strip()
        # Some model UIs flatten the entire five-factor answer into a single paragraph.
        # Ignore the section heading and unrelated content before the first factor.
        if evidence:
            severity = match.group("severity").casefold().replace(" ", "_").replace("-", "_")
            result.setdefault(key, {"severity": severity, "evidence": evidence, "source": FACTOR_SOURCES[key]})
    return result


def has_complete_response_scorecard(response_text: str, review: dict | None = None) -> bool:
    """Return true only when the response includes a score and all five factors."""
    review = review or {}
    has_score = (
        "visual_opportunity_score" in review
        or re.search(r"^\s*Visual opportunity score\s*:", response_text, re.IGNORECASE | re.MULTILINE)
    )
    factors = parse_factor_assessments(response_text, review)
    return bool(has_score and set(FACTORS).issubset(factors))


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
    interactions = audit.get("broken_links_and_interactions") or {}
    interaction_audit = interactions.get("interactions") or {}
    interaction_not_applicable = (
        interaction_audit.get("status") == "not_applicable"
        and interaction_audit.get("tested_count") == 0
        and not interaction_audit.get("findings")
    )
    if interactions_status in {"clear", "issues_found"} and interaction_not_applicable:
        # The audit explicitly found no safe menu/tab/disclosure control to test.
        # That is not a failed interaction and should not make the whole scorecard uncertain.
        factors["interactions"] = {
            "severity": "none",
            "evidence": "The audit found no safe menu, tab, or disclosure controls to test; no failed interaction was observed.",
            "source": "script_audit",
        }
    elif (interactions_status not in {"clear", "issues_found"}
          or interaction_audit.get("status") not in {"clear", "issues_found"}):
        factors["interactions"] = {
            "severity": "not_assessable",
            "evidence": f"Automated links/interactions audit status is {interactions_status}; findings are unavailable.",
            "source": "script_audit",
        }
    return factors


def normalize_and_score(factors: dict, audit: dict | None = None) -> dict:
    """Validate factor evidence and score named issues, with a deterministic absent-hero floor."""
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
    points = {key: SEVERITY_POINTS[item["severity"]] for key, item in normalized.items()}
    hero = (audit or {}).get("hero_media") or {}
    hero_status = str(hero.get("status", "unknown")).casefold()
    hero_absence_points = 45 if hero_status == "absent" else 0
    if hero_absence_points:
        # The missing first-view image/video is one named issue. It replaces a
        # weaker model rating for that same root cause and never double-counts it.
        points["hero_imagery"] = max(points.get("hero_imagery", 0), hero_absence_points)
    score = min(100, sum(points.values()))
    verdict = "pass" if score < 50 else "medium" if score < 75 else "high"
    return {"errors": [], "factors": normalized, "factor_points": points,
            "hero_media_absence_points": hero_absence_points,
            "hero_media_status": hero_status,
            "score": score, "verdict": verdict, "uncertain_factors": []}
