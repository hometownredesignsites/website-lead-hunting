"""Shared factor-based visual opportunity scoring contract."""
from __future__ import annotations

import re


ASSESSMENT_VERSION = "fixed-factors-v13"

FACTORS = {
    "hero_imagery": "Hero imagery",
    "image_quality": "Image quality",
    "design_freshness": "Design freshness",
    "readability": "Readability",
    "layout_mobile": "Layout and mobile use",
    "broken_images": "Broken images",
    "interactions": "Interactions",
}
SEVERITY_POINTS = {"none": 0, "minor": 5, "moderate": 25, "severe": 50}
FACTOR_SOURCES = {
    "hero_imagery": "screenshots", "image_quality": "screenshots",
    "design_freshness": "screenshots", "readability": "screenshots",
    "layout_mobile": "screenshots", "broken_images": "script_audit",
    "interactions": "script_audit",
}


def classify_broken_image_audit(images: dict | None) -> dict:
    """Separate source-backed visible failures from ambiguous browser noise."""
    images = images or {}
    records = images.get("images") or []
    request_failures = images.get("request_failures") or []
    explicitly_unattributed = images.get("unattributed_request_failures") or []
    explicitly_unresolved = images.get("unresolved_images") or []
    transient_failures = images.get("transient_request_failures") or []

    def usable_url(value) -> str:
        value = str(value or "").strip()
        return value if value.startswith(("http://", "https://", "data:image/", "blob:")) else ""

    referenced_urls = set()
    for item in records:
        if isinstance(item, dict):
            for key in ("url", "current_url", "declared_url"):
                value = usable_url(item.get(key))
                if value:
                    referenced_urls.add(value)

    confirmed_records = []
    unresolved_records = list(explicitly_unresolved)
    for item in records:
        if not isinstance(item, dict):
            continue
        url = next((usable_url(item.get(key)) for key in ("url", "current_url", "declared_url")
                    if usable_url(item.get(key))), "")
        if url and item.get("visible", True) is not False:
            confirmed_records.append(item)
        elif not url:
            unresolved_records.append(item)

    matched_failures = [item for item in request_failures
                        if isinstance(item, dict) and usable_url(item.get("url")) in referenced_urls]
    unmatched_failures = list(explicitly_unattributed) + [
        item for item in request_failures
        if not isinstance(item, dict) or usable_url(item.get("url")) not in referenced_urls
    ]
    confirmed_urls = {next((usable_url(item.get(key)) for key in ("url", "current_url", "declared_url")
                            if usable_url(item.get(key))), "") for item in confirmed_records}
    confirmed_urls.update(usable_url(item.get("url")) for item in matched_failures
                          if isinstance(item, dict) and usable_url(item.get("url")))

    # Old ledgers only retained a count. Without item-level provenance, do not
    # reinterpret a nonzero count as harmless browser noise.
    legacy_unexplained_count = (
        not records and not request_failures and int(images.get("failed_image_count", 0) or 0) > 0
    )
    confirmed_count = max(len(confirmed_urls), int(images.get("confirmed_failed_image_count", 0) or 0))
    if legacy_unexplained_count:
        confirmed_count = max(confirmed_count, int(images.get("failed_image_count", 0) or 0))
    ambiguous_count = len(unresolved_records) + len(unmatched_failures) + len(transient_failures)
    raw_count = int(images.get("reported_failed_image_count", images.get("failed_image_count", 0)) or 0)
    raw_count = max(raw_count, len(records) + len(request_failures) + len(transient_failures))
    fallback_allowed = confirmed_count == 0
    status = str(images.get("status", "not_run")).casefold()
    if fallback_allowed and ambiguous_count and status == "issues_found":
        status = "screenshot_fallback"
    return {
        "status": status,
        "confirmed_failed_image_count": confirmed_count,
        "reported_failed_image_count": raw_count,
        "unresolved_images": unresolved_records,
        "unattributed_request_failures": unmatched_failures,
        "transient_request_failures": transient_failures,
        "screenshot_fallback_allowed": bool(fallback_allowed),
    }


def _source_matches(key: str, source) -> bool:
    """Accept common harmless spellings while enforcing factor provenance."""
    value = re.sub(r"[^a-z]+", "_", str(source or "").casefold()).strip("_")
    expected = FACTOR_SOURCES[key]
    aliases = {"screenshots": {"screenshots", "screenshot", "visual", "visual_review", "image_review"},
               "script_audit": {"script_audit", "script", "audit", "automated_audit"}}
    if value == "screenshots_and_script_audit":
        return True
    return value in aliases[expected]


def parse_factor_assessments(response_text: str, review: dict) -> dict:
    """Parse fixed factor assessments from labeled text or JSON review data."""
    factors = review.get("factor_assessments") or review.get("factors")
    result = {}
    if isinstance(factors, dict):
        for key, item in factors.items():
            if isinstance(item, dict):
                result[key] = {**item, "state_explicit": bool(item.get("state"))}
    label_to_key = {label.casefold(): key for key, label in FACTORS.items()}
    labels = "|".join(re.escape(label) for label in FACTORS.values())
    pattern = re.compile(
        rf"(?P<label>{labels})\s*:\s*(?:(?P<state>assessed|not[_ -]applicable|uncertain)(?:\s*\((?P<severity>none|minor|moderate|severe)\))?|(?P<legacy_severity>none|minor|moderate|severe|not[_ -]assessable))\s*(?:—|--|-|�)\s*",
        flags=re.IGNORECASE,
    )
    matches = list(pattern.finditer(response_text))
    for index, match in enumerate(matches):
        key = label_to_key[match.group("label").casefold()]
        evidence_end = matches[index + 1].start() if index + 1 < len(matches) else len(response_text)
        evidence = response_text[match.end():evidence_end].strip()
        # Model responses often add sentence punctuation after the required
        # source tag (for example, ``Source=screenshots.``). Accept that
        # harmless formatting while keeping the tag anchored at the end so
        # arbitrary prose cannot satisfy the source requirement.
        source_match = re.search(
            r"\s*;?\s*source\s*=\s*(screenshots_and_script_audit|screenshots?|script[_ -]?audit|script|visual(?:[_ -]review)?|image[_ -]review|audit|automated[_ -]audit)"
            r"(?:\s*\([^)]*\))?"
            r"(?:\s+and\s+page[- ]flow measurements)?"
            r"(?:\s*;\s*(?:supplementary|supporting)\s+page[- ]flow evidence\s*=\s*script_audit)?"
            r"\s*[.,;:!?]*\s*$",
            evidence,
            flags=re.I,
        )
        source = source_match.group(1).casefold() if source_match else None
        if source_match:
            evidence = evidence[:source_match.start()].strip()
        # Some model UIs flatten the entire factor answer into a single paragraph.
        # Ignore the section heading and unrelated content before the first factor.
        if evidence:
            state = match.group("state")
            severity = match.group("severity") or match.group("legacy_severity")
            state = (state or ("uncertain" if severity and severity.casefold().replace("-", "_") == "not_assessable" else "assessed"))
            state = state.casefold().replace(" ", "_").replace("-", "_")
            severity = severity.casefold().replace(" ", "_").replace("-", "_") if severity else None
            # Prefer the human-readable factor lines when both they and a
            # structured object are present. Chat UIs sometimes serialize a
            # stale or malformed JSON copy alongside a correct text answer.
            result[key] = {"state": state, "state_explicit": bool(match.group("state")),
                           "severity": severity, "evidence": evidence, "source": source}
    return result


def has_complete_response_scorecard(response_text: str, review: dict | None = None) -> bool:
    """Return true only when the response has a factor-only, evidence-complete contract."""
    review = review or {}
    if "visual_opportunity_score" in review or "visual_verdict" in review:
        return False
    if re.search(r"^\s*(?:Visual opportunity score|Verdict)\s*:", response_text,
                 re.IGNORECASE | re.MULTILINE):
        return False
    factors = parse_factor_assessments(response_text, review)
    if set(factors) != set(FACTORS):
        return False
    for key in FACTORS:
        item = factors[key]
        if not isinstance(item, dict):
            return False
        if not item.get("state_explicit"):
            return False
        if item.get("state") not in {"assessed", "not_applicable", "uncertain"}:
            return False
        if not isinstance(item.get("evidence"), str) or not item["evidence"].strip():
            return False
        if not item.get("source") or not _source_matches(key, item.get("source")):
            return False
        if item.get("state") == "assessed" and item.get("severity") not in SEVERITY_POINTS:
            return False
        if item.get("state") != "assessed" and item.get("severity") is not None:
            return False
    return True


def has_prominent_first_view_video(audit: dict | None) -> bool:
    """Recognize a substantial first-view video even when no frame decoded."""
    hero = (audit or {}).get("hero_media") or {}
    for view in (hero.get("desktop") or {}, hero.get("mobile") or {}):
        for media in view.get("media_candidates") or []:
            if not isinstance(media, dict) or str(media.get("type", "")).casefold() != "video":
                continue
            try:
                width = float(media.get("visibleWidth", media.get("width", 0)) or 0)
                height = float(media.get("visibleHeight", media.get("height", 0)) or 0)
                area_percent = float(media.get("viewportAreaPercent", media.get("viewport_area_percent", 0)) or 0)
            except (TypeError, ValueError):
                continue
            if media.get("prominent") is True or (width >= 120 and height >= 90 and area_percent >= 8):
                return True
    return False


def apply_first_view_video_overrides(factors: dict, audit: dict | None) -> bool:
    """Make a qualifying hero video neutral for hero and image-quality factors."""
    if not has_prominent_first_view_video(audit):
        return False
    evidence = (
        "Script measurements identify a substantial video element in the first viewport. Per the rubric, it is "
        "assumed to play and can show multiple examples; hero imagery and image quality therefore pass with zero "
        "points, even when Chromium did not decode a frame or the still screenshots omit project photos."
    )
    for key in ("hero_imagery", "image_quality"):
        factors[key] = {
            "state": "assessed", "severity": "none", "evidence": evidence,
            "source": "screenshots_and_script_audit",
        }
    return True


def _screenshot_explicitly_finds_no_broken_images(item: dict | None) -> bool:
    item = item or {}
    source = re.sub(r"[^a-z]+", "_", str(item.get("source") or "").casefold()).strip("_")
    evidence = str(item.get("evidence") or "")
    if "screenshot" not in source or "screenshot" not in evidence.casefold():
        return False
    no_failure = re.search(
        r"\b(?:no|none|not visible|not apparent|not observed|cannot see|can't see)\b"
        r".{0,100}\b(?:broken|failed|missing)\s+(?:image|photo|picture)s?\b"
        r"|\b(?:broken|failed|missing)\s+(?:image|photo|picture)s?\b.{0,100}\b(?:not visible|not apparent|not observed|none visible)\b",
        evidence, re.IGNORECASE | re.DOTALL,
    )
    return bool(no_failure)


def enforce_script_audit_evidence(factors: dict, audit: dict, screenshots_available: bool = False) -> dict:
    """Prevent unavailable automated checks from being rated as clean."""
    apply_first_view_video_overrides(factors, audit)
    image_status = (audit.get("broken_images") or {}).get("status", "not_run")
    images = audit.get("broken_images") or {}
    image_classification = classify_broken_image_audit(images)
    loading = images.get("lazy_loading") or {}
    pending_non_lazy = any(
        str((item or {}).get("loading", "")).casefold() != "lazy"
        for item in images.get("pending_images", []) or []
    )
    sample_sufficient = bool(loading.get("sample_sufficient")) or (
        int(loading.get("attempted", 0) or 0) >= 20
    )
    all_lazy_images_inspected = bool(loading.get("complete"))
    confirmed_failures = image_classification["confirmed_failed_image_count"]
    image_sample_accepted = (
        (image_status == "clear" and not pending_non_lazy)
        or (all_lazy_images_inspected and not pending_non_lazy)
        or (image_status == "sampled" and sample_sufficient and not pending_non_lazy)
    )
    fallback_available = screenshots_available and (
        bool(images.get("screenshot_fallback_allowed")) or image_classification["screenshot_fallback_allowed"]
    )
    factor = factors.get("broken_images") or {}
    screenshot_fallback_assessment = False
    if fallback_available and _screenshot_explicitly_finds_no_broken_images(factor):
        factors["broken_images"] = {
            "state": "assessed", "severity": "none",
            "evidence": ("The script audit had only unresolved or uncorrelated image evidence. All four screenshots "
                         "were uploaded and show no visible broken image; this finding is limited to the captured views."),
            "source": "screenshots_and_script_audit",
        }
        screenshot_fallback_assessment = True
    elif fallback_available and factor.get("state") == "assessed" and "screenshot" in str(factor.get("source", "")).casefold():
        # A model assessment grounded in the supplied screenshots is a valid
        # fallback when the automated image check is inconclusive.
        screenshot_fallback_assessment = True
    elif image_sample_accepted and not confirmed_failures:
        factors["broken_images"] = {
            "state": "assessed", "severity": "none",
            "evidence": ("The script inspected every discovered lazy-loaded image and found no confirmed broken images."
                         if all_lazy_images_inspected else
                         "The script completed its image audit with no confirmed broken images."
                         if image_status == "clear" else
                         f"The script inspected a sufficient sample of {loading.get('attempted', 20)} lazy-loaded images; "
                         "no confirmed broken images were found."),
            "source": "script_audit",
        }
    elif image_status not in {"clear", "issues_found"} and not screenshot_fallback_assessment:
        factors["broken_images"] = {
            "state": "uncertain", "severity": None,
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
    available_count = interaction_audit.get("available_count")
    tested_count = interaction_audit.get("tested_count", 0)
    no_controls_to_test = (interaction_not_applicable and
                           (available_count == 0 or available_count is None))
    if interactions_status in {"clear", "issues_found"} and no_controls_to_test:
        # No safe controls is explicitly not applicable and contributes zero points.
        factors["interactions"] = {
            "state": "not_applicable", "severity": None,
            "evidence": "The audit found no safe menu, tab, or disclosure controls to test.",
            "source": "script_audit",
        }
    elif available_count is not None and available_count > tested_count:
        factors["interactions"] = {
            "state": "uncertain", "severity": None,
            "evidence": f"{available_count} safe controls were found but only {tested_count} were tested; interaction evidence is incomplete.",
            "source": "script_audit",
        }
    elif (interactions_status not in {"clear", "issues_found"}
          or interaction_audit.get("status") not in {"clear", "issues_found"}):
        factors["interactions"] = {
            "state": "uncertain", "severity": None,
            "evidence": f"Automated links/interactions audit status is {interactions_status}; findings are unavailable.",
            "source": "script_audit",
        }
    return factors


def normalize_and_score(factors: dict, audit: dict | None = None) -> dict:
    """Validate factor evidence and apply deterministic floors for major redesign signals."""
    factors = dict(factors or {})
    hero_video_present = apply_first_view_video_overrides(factors, audit or {})
    hero = (audit or {}).get("hero_media") or {}
    hero_status = "present" if hero_video_present else str(hero.get("status", "unknown")).casefold()
    hero_absence_points = 45 if hero_status == "absent" else 0
    normalized = {}
    errors = []
    uncertain = []
    unexpected = sorted(set(factors) - set(FACTORS))
    if unexpected:
        errors.append("unexpected scoring factor(s): " + ", ".join(unexpected))
    for key, label in FACTORS.items():
        item = factors.get(key)
        if not isinstance(item, dict):
            errors.append(f"required factor assessment missing: {key}")
            continue
        state = str(item.get("state", "")).strip().casefold().replace(" ", "_").replace("-", "_")
        severity_value = item.get("severity")
        severity = str(severity_value or "").strip().casefold().replace(" ", "_").replace("-", "_")
        evidence = item.get("evidence")
        source = item.get("source")
        if state not in {"assessed", "not_applicable", "uncertain"}:
            errors.append(f"{key} state must be assessed, not_applicable, or uncertain")
            continue
        if state == "assessed" and severity not in SEVERITY_POINTS:
            errors.append(f"{key} assessed severity must be none, minor, moderate, or severe")
            continue
        if state in {"not_applicable", "uncertain"} and severity:
            errors.append(f"{key} {state} assessment must not include a severity")
            continue
        if not isinstance(evidence, str) or not evidence.strip():
            errors.append(f"{key} requires specific supporting evidence or a reason it cannot be assessed")
            continue
        # The source is fixed by the factor schema. Preserve any model supplied
        # label for diagnostics, but do not reject useful evidence over a
        # harmless label variation (or a wrong label in the parallel JSON).
        reported_source = source
        canonical_source = FACTOR_SOURCES[key]
        normalized_source = re.sub(r"[^a-z]+", "_", str(source or "").casefold()).strip("_")
        if key == "broken_images" and normalized_source == "screenshots_and_script_audit":
            canonical_source = normalized_source
        if state == "uncertain":
            uncertain.append(key)
        normalized[key] = {
            "label": label,
            "state": state,
            "severity": severity if state == "assessed" else None,
            "evidence": evidence.strip(),
            "source": canonical_source,
            "reported_source": reported_source,
        }
    if errors:
        return {"errors": errors, "factors": normalized, "score": None, "verdict": "uncertain",
                "uncertain_factors": uncertain, "hero_media_status": hero_status,
                "hero_media_absence_points": hero_absence_points, "hero_video_present": hero_video_present}
    if uncertain:
        return {"errors": [], "factors": normalized, "score": None, "verdict": "uncertain",
                "uncertain_factors": uncertain, "hero_media_status": hero_status,
                "hero_media_absence_points": hero_absence_points, "hero_video_present": hero_video_present}
    points = {key: SEVERITY_POINTS[item["severity"]] if item["state"] == "assessed" else 0
              for key, item in normalized.items()}
    dated_design_severity = normalized["design_freshness"]["severity"]
    dated_design_floor_points = 45 if dated_design_severity in {"moderate", "severe"} else 0
    copyright_signal = (audit or {}).get("copyright_footer_signal") or {}
    current_year = copyright_signal.get("current_year")
    displayed_years = copyright_signal.get("displayed_years") or []
    stale_years = [year for year in displayed_years
                   if isinstance(current_year, int) and isinstance(year, int) and year < current_year]
    oldest_year_gap = max((current_year - year for year in stale_years), default=0) if isinstance(current_year, int) else 0
    stale_copyright_points = 10 if oldest_year_gap > 2 else 5 if oldest_year_gap else 0
    if hero_absence_points:
        # The missing first-view image/video is one named issue. It replaces a
        # weaker model rating for that same root cause and never double-counts it.
        points["hero_imagery"] = max(points.get("hero_imagery", 0), hero_absence_points)
    if hero_video_present:
        # User rubric: a prominent first-view video is assumed to play and
        # makes the hero-imagery factor neutral regardless of model judgment.
        points["hero_imagery"] = 0
    if dated_design_floor_points:
        # A materially dated overall design is independently a substantial
        # redesign opportunity, on par with a confirmed missing hero image.
        points["design_freshness"] = max(points.get("design_freshness", 0), dated_design_floor_points)
    score = min(100, sum(points.values()) + stale_copyright_points)
    verdict = "pass" if score < 50 else "medium" if score < 75 else "high"
    return {"errors": [], "factors": normalized, "factor_points": points,
            "hero_media_absence_points": hero_absence_points,
            "hero_media_status": hero_status,
            "hero_video_present": hero_video_present,
            "dated_design_floor_points": dated_design_floor_points,
            "stale_copyright_points": stale_copyright_points,
            "score": score, "verdict": verdict, "uncertain_factors": []}
