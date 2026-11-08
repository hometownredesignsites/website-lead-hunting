"""Shared replicate aggregation rules for durable scorecards and live ledgers."""
from __future__ import annotations

import statistics
from typing import Any


DISAGREEMENT_THRESHOLD_POINTS = 30

_DESIGN_CUE_GROUPS = {
    "typography": ("serif", "sans-serif", "typeface", "typography", "font", "oversized"),
    "panels": ("panel", "boxed", "black content", "heavy background", "framed"),
    "controls": ("button", "rectangular control", "boxy control", "navigation control"),
    "ornamentation": ("gold accent", "gradient", "border", "shadow", "texture", "ornament"),
    "layout": ("spacing", "hierarchy", "layout", "grid", "clutter", "density"),
    "imagery": ("low-resolution", "pixelated", "poor crop", "stretched image", "image quality"),
}
_DESIGN_AREAS = (
    "hero", "desktop_top", "mobile_top", "gallery", "portfolio", "contact",
    "navigation", "footer", "service section", "desktop_mid", "mobile_mid",
)
_FACTOR_CUES = {
    "hero_imagery": ("hero", "first viewport", "top screenshot", "prominent image", "video", "photograph"),
    "image_quality": ("blur", "pixelat", "resolution", "crop", "stretched", "sharp", "clear", "image"),
    "readability": ("contrast", "legible", "readable", "text", "font size"),
    "layout_mobile": ("mobile", "clipping", "overlap", "overflow", "viewport", "layout", "navigation"),
    "broken_images": ("broken image", "failed image", "image request", "screenshot", "source"),
    "interactions": ("anchor", "link", "button", "menu", "control", "interaction", "tab", "form"),
}
_SCREENSHOT_REFERENCES = (
    "desktop_top", "desktop_mid", "mobile_top", "mobile_mid", "hero", "gallery",
    "portfolio", "contact", "navigation", "footer", "first viewport", "mobile view",
)
_GENERIC_ONLY = (
    "looks dated", "feels dated", "looks old", "feels old", "dated design",
    "outdated design", "not modern", "poor design",
)


def factor_points_for(review: dict[str, Any]) -> dict[str, int]:
    calibration = review.get("score_calibration") or {}
    factors = calibration.get("factor_points") or {}
    return {
        key: int(value.get("points", 0))
        for key, value in factors.items()
        if isinstance(value, dict) and isinstance(value.get("points", 0), (int, float))
    }


def has_specific_scored_factor_evidence(review: dict[str, Any]) -> bool:
    """Require validated, concrete evidence for at least one scored factor."""
    if review.get("scorecard_validation_status") != "valid":
        return False
    points = factor_points_for(review)
    factors = review.get("factor_assessments") or review.get("factors") or {}
    scored = [key for key, value in points.items() if value > 0]
    if not scored:
        return False

    for key in scored:
        factor = factors.get(key) or {}
        evidence = str(factor.get("evidence") or "").strip()
        normalized = " ".join(evidence.casefold().split())
        if factor.get("state") != "assessed" or len(evidence) < 60:
            continue
        if any(normalized == phrase for phrase in _GENERIC_ONLY):
            continue
        if key == "design_freshness":
            matched_groups = sum(
                any(cue in normalized for cue in cues)
                for cues in _DESIGN_CUE_GROUPS.values()
            )
            matched_areas = sum(area in normalized for area in _DESIGN_AREAS)
            # A dated-design override needs more than a taste judgment: multiple
            # observable patterns must recur across at least two page areas.
            if matched_groups < 2 or matched_areas < 2:
                continue
        else:
            if not any(cue in normalized for cue in _FACTOR_CUES.get(key, ())):
                continue
            if factor.get("source") in {"screenshots", "screenshots_and_script_audit"} and not any(
                area in normalized for area in _SCREENSHOT_REFERENCES
            ):
                continue
        return True
    return False


def choose_aggregate_values(scores: list[int], highest_evidence_supported: bool) -> dict[str, Any]:
    """Use the maximum on large, evidence-backed disagreement; otherwise mean."""
    scores = [int(score) for score in scores]
    exact_mean = statistics.mean(scores)
    rounded_mean = int(exact_mean + 0.5)
    score_range = max(scores) - min(scores)
    trigger = score_range >= DISAGREEMENT_THRESHOLD_POINTS
    highest_index = max(range(len(scores)), key=scores.__getitem__)
    applied = trigger and highest_evidence_supported
    score = scores[highest_index] if applied else rounded_mean
    return {
        "score": score,
        "mean": exact_mean,
        "range": score_range,
        "method": "highest_valid_replicate_on_range_gte_30" if applied else "arithmetic_mean_rounded_half_up",
        "disagreement_triggered": trigger,
        "override_applied": applied,
        "disagreement_review_required": trigger and not highest_evidence_supported,
        "selected_replicate": highest_index + 1 if applied else None,
        "selected_score": scores[highest_index] if applied else None,
        "evidence_supported": highest_evidence_supported,
        "threshold_points": DISAGREEMENT_THRESHOLD_POINTS,
    }


def choose_aggregate_score(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate validated replicate reviews using the shared threshold rule."""
    scores = [int(review["visual_opportunity_score"]) for review in reviews]
    highest_index = max(range(len(scores)), key=scores.__getitem__)
    evidence_supported = has_specific_scored_factor_evidence(reviews[highest_index])
    return choose_aggregate_values(scores, evidence_supported)
