#!/usr/bin/env python3
"""Update the provider's live score CSV after one visual replicate."""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from urllib.parse import urlparse

from visual_replicate_aggregation import (choose_aggregate_values,
                                          has_specific_scored_factor_evidence)
from location_format import format_location
try:
    from .contact_formatting import normalize_phone
except ImportError:
    from contact_formatting import normalize_phone


FACTORS = (
    "hero_imagery", "image_quality", "design_freshness", "readability",
    "layout_mobile", "broken_images", "interactions",
)
REPLICATES = (1, 2, 3)
MASTER_FIELDS = [
    "website_key", "candidate_id", "business_name", "website_url",
    "category", "location", "email", "phone", "contact_status",
    "ContactSource", "provider", "assessment_key", "source_run_key",
    "source_lead_hunt_run_id", "review_workflow_run_id", "confidence_level",
    "replicates_required",
]
for _replicate in REPLICATES:
    MASTER_FIELDS.extend((f"replicate_{_replicate}_status", f"replicate_{_replicate}_score",
                          f"replicate_{_replicate}_factor_points_json",
                          f"replicate_{_replicate}_evidence_supported"))
MASTER_FIELDS.extend((
    "average_score_exact", "score", "average_factor_points_json",
    "applied_factor_points_json", "applied_replicate", "score_range", "score_aggregation",
    "disagreement_review_required",
    "provisional_outcome", "final_outcome", "assessment_state",
    "latest_replicate_status", "updated_at", "actions_run_url",
))

HIGH_MEDIUM_OMIT_FIELDS = {
    "replicates_required",
    "replicate_1_status", "replicate_2_status", "replicate_3_status",
    "replicate_1_factor_points_json", "replicate_2_factor_points_json", "replicate_3_factor_points_json",
    "replicate_1_evidence_supported", "replicate_2_evidence_supported", "replicate_3_evidence_supported",
    "average_score_exact", "applied_factor_points_json", "applied_replicate", "score_range",
    "score_aggregation", "disagreement_review_required",
}
HIGH_MEDIUM_FIELDS = [field for field in MASTER_FIELDS if field not in HIGH_MEDIUM_OMIT_FIELDS]
HIGH_MEDIUM_FIELDS.append("Preview_URLs_json")


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], check=check,
                          text=True, capture_output=True)


def canonical_key(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host:
        raise ValueError(f"Candidate has no valid website host: {url!r}")
    return host


def load_rows(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        rows = {}
        for row in reader:
            key = (row.get("website_key") or "").strip()
            if key:
                rows[key] = {field: row.get(field, "") or "" for field in MASTER_FIELDS}
                rows[key]["phone"] = normalize_phone(rows[key].get("phone", ""))
        return rows


def verdict(score: int) -> str:
    return "pass" if score < 50 else "medium" if score < 75 else "high"


def serialize_points(points: dict[str, float]) -> str:
    return json.dumps({key: round(value, 2) for key, value in sorted(points.items())},
                      separators=(",", ":"), sort_keys=True)


def apply_event(repo: Path, event: dict) -> dict:
    scores_dir = repo / "scores"
    scores_dir.mkdir(parents=True, exist_ok=True)
    master_path = scores_dir / "SCORING_MASTER.csv"
    high_medium_path = scores_dir / "HIGH_MEDIUM_CANDIDATES.csv"
    rows = load_rows(master_path)

    context = event["context"]
    candidate_id = str(context["candidate_id"])
    website_url = str(context["website_url"])
    key = canonical_key(website_url)
    provider = event["provider"]
    assessment_key = f"{event['review_run_id']}:{event['source_run_key']}:{event['source_run_id']}:{key}"
    row = rows.get(key, {field: "" for field in MASTER_FIELDS})
    if row.get("assessment_key") != assessment_key:
        row = {field: "" for field in MASTER_FIELDS}
        row.update({
            "website_key": key,
            "candidate_id": candidate_id,
            "provider": provider,
            "assessment_key": assessment_key,
            "source_run_key": event["source_run_key"],
            "source_lead_hunt_run_id": event["source_run_id"],
            "replicates_required": "3",
        })
    row.update({
        "website_url": website_url,
        "business_name": str(context.get("business_name") or row.get("business_name") or ""),
        "category": str(context.get("category") or row.get("category") or ""),
        "location": str(context.get("location") or row.get("location") or ""),
        "email": str(context.get("email") or row.get("email") or ""),
        "phone": normalize_phone(context.get("phone") or row.get("phone") or ""),
        "contact_status": str(context.get("contact_status") or row.get("contact_status") or "not_found"),
        "ContactSource": str(context.get("ContactSource") or context.get("email_source_url") or row.get("ContactSource") or ""),
        "provider": provider,
        "source_run_key": event["source_run_key"],
        "source_lead_hunt_run_id": event["source_run_id"],
        "review_workflow_run_id": event["review_run_id"],
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "actions_run_url": event["actions_run_url"],
    })

    index = event["replicate"]
    status = event["attempt_status"].get("status", "retry-needed")
    result = event.get("result") or {}
    review = result.get("review") if isinstance(result, dict) else None
    raw_score = review.get("visual_opportunity_score") if isinstance(review, dict) else None
    valid_score = (status == "passed" and isinstance(result, dict)
                   and result.get("status") == "passed" and isinstance(review, dict)
                   and isinstance(raw_score, int) and not isinstance(raw_score, bool)
                   and 0 <= raw_score <= 100
                   and review.get("scorecard_validation_status") == "valid"
                   and review.get("site_outcome") in {"pass", "medium", "high"})
    if valid_score:
        row[f"replicate_{index}_status"] = "passed"
        row[f"replicate_{index}_score"] = str(raw_score)
        calibration = review.get("score_calibration") or {}
        factor_points = {
            factor: (calibration.get("factor_points") or {}).get(factor, {}).get("points", 0)
            for factor in FACTORS
        }
        row[f"replicate_{index}_factor_points_json"] = serialize_points(factor_points)
        row[f"replicate_{index}_evidence_supported"] = str(
            has_specific_scored_factor_evidence(review)
        ).lower()
    elif not row.get(f"replicate_{index}_score"):
        row[f"replicate_{index}_status"] = status if status in {"retry-needed", "failed"} else "uncertain"

    valid_indices = [replicate for replicate in REPLICATES
                     if row.get(f"replicate_{replicate}_score", "") != ""]
    scores = [int(row[f"replicate_{replicate}_score"]) for replicate in valid_indices]
    confidence = len(scores)
    row["confidence_level"] = str(confidence)
    row["latest_replicate_status"] = "passed" if valid_score else status
    if scores:
        exact = mean(scores)
        highest_index = max(range(len(scores)), key=scores.__getitem__)
        highest_replicate = valid_indices[highest_index]
        highest_evidence_supported = (
            row.get(f"replicate_{highest_replicate}_evidence_supported") == "true"
        )
        aggregation = choose_aggregate_values(scores, highest_evidence_supported)
        score = aggregation["score"]
        row["average_score_exact"] = f"{exact:.2f}"
        row["score"] = str(score)
        row["score_range"] = str(aggregation["range"])
        row["score_aggregation"] = aggregation["method"]
        row["disagreement_review_required"] = str(bool(aggregation.get("disagreement_review_required"))).lower()
        row["applied_replicate"] = (
            str(valid_indices[aggregation["selected_replicate"] - 1])
            if aggregation.get("override_applied") else ""
        )
        row["provisional_outcome"] = verdict(score)
        factor_values: dict[str, list[float]] = {factor: [] for factor in FACTORS}
        for replicate in valid_indices:
            raw = row.get(f"replicate_{replicate}_factor_points_json") or "{}"
            try:
                values = json.loads(raw)
            except json.JSONDecodeError:
                values = {}
            for factor in FACTORS:
                factor_values[factor].append(float(values.get(factor, 0)))
        row["average_factor_points_json"] = serialize_points(
            {factor: mean(points) for factor, points in factor_values.items() if points}
        )
        applied_points = factor_values
        if aggregation.get("override_applied") and aggregation.get("selected_replicate"):
            selected = valid_indices[aggregation["selected_replicate"] - 1]
            raw = row.get(f"replicate_{selected}_factor_points_json") or "{}"
            try:
                selected_points = json.loads(raw)
            except json.JSONDecodeError:
                selected_points = {}
            applied_points = {key: [float(selected_points.get(key, 0))] for key in FACTORS}
        row["applied_factor_points_json"] = serialize_points(
            {factor: values[0] for factor, values in applied_points.items() if values}
        )
    else:
        row["average_score_exact"] = ""
        row["score"] = ""
        row["provisional_outcome"] = "uncertain"
        row["average_factor_points_json"] = "{}"
        row["applied_factor_points_json"] = "{}"
        row["score_range"] = ""
        row["score_aggregation"] = ""
        row["applied_replicate"] = ""
        row["disagreement_review_required"] = "false"
    row["final_outcome"] = row.get("provisional_outcome", "") if confidence == 3 else ""
    if confidence == 3:
        row["assessment_state"] = "complete"
    elif not valid_score:
        row["assessment_state"] = "retry-needed"
    else:
        row["assessment_state"] = "in-progress"
    rows[key] = row

    ordered = sorted(rows.values(), key=lambda item: (
        -int(item["score"]) if item.get("score", "").isdigit() else 1,
        (item.get("business_name") or "").casefold(),
        item.get("website_key", ""),
    ))
    previous_preview_urls: dict[str, str] = {}
    if high_medium_path.is_file():
        with high_medium_path.open("r", encoding="utf-8-sig", newline="") as source:
            for previous in csv.DictReader(source):
                key = (previous.get("website_key") or "").strip()
                if key:
                    previous_preview_urls[key] = previous.get("Preview_URLs_json") or "[]"

    high_medium_rows = []
    for item in ordered:
        if item.get("provisional_outcome") in {"high", "medium"}:
            key = item.get("website_key", "")
            high_medium_rows.append({
                **item,
                "Preview_URLs_json": previous_preview_urls.get(key, "[]"),
            })
    for path, selected, fields in (
        (master_path, ordered, MASTER_FIELDS),
        (high_medium_path, high_medium_rows, HIGH_MEDIUM_FIELDS),
    ):
        with path.open("w", encoding="utf-8", newline="") as destination:
            writer = csv.DictWriter(destination, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(selected)
    return row


def commit_update(repo: Path, branch: str, event: dict) -> dict:
    token = os.environ.get("PRIVATE_RESULTS_TOKEN", "")
    if not token:
        raise RuntimeError("PRIVATE_RESULTS_TOKEN is not set")
    git(repo, "config", "user.name", "github-actions[bot]")
    git(repo, "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    auth = ["-c", f"http.extraheader=AUTHORIZATION: basic {encoded}"]
    update = {}
    for attempt in range(6):
        if attempt:
            fetched = subprocess.run(["git", "-C", str(repo), *auth, "fetch", "origin", branch],
                                     check=False, text=True, capture_output=True)
            if fetched.returncode:
                raise RuntimeError(f"Could not refresh private scoring branch: {fetched.stderr.strip()}")
            git(repo, "reset", "--hard", "FETCH_HEAD")
        update = apply_event(repo, event)
        git(repo, "add", "--", "scores/SCORING_MASTER.csv", "scores/HIGH_MEDIUM_CANDIDATES.csv")
        staged = git(repo, "diff", "--cached", "--quiet", check=False)
        if staged.returncode == 0:
            break
        git(repo, "commit", "-m", f"Update {event['provider']} scoring ledger: {update['website_key']} replicate {event['replicate']}")
        pushed = subprocess.run(["git", "-C", str(repo), *auth, "push", "origin",
                                 f"HEAD:refs/heads/{branch}"], check=False, text=True, capture_output=True)
        if pushed.returncode == 0:
            break
        detail = (pushed.stderr or pushed.stdout).strip()
        if not any(value in detail.lower() for value in ("non-fast-forward", "fetch first", "rejected")):
            raise RuntimeError(f"Could not push private scoring ledger update: {detail}")
        if attempt == 5:
            raise RuntimeError(f"Private scoring branch kept advancing after six retries: {detail}")
        print(f"Private scoring branch advanced concurrently; replaying this replicate update ({attempt + 1}/5).")
    return update


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-repo", type=Path, required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--provider", choices=("ChatGPT", "Duck.ai"), required=True)
    parser.add_argument("--candidate-context", type=Path, required=True)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--attempt-status", type=Path, required=True)
    parser.add_argument("--replicate", type=int, choices=REPLICATES, required=True)
    parser.add_argument("--source-run-key", required=True)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--review-run-id", required=True)
    parser.add_argument("--actions-run-url", required=True)
    parser.add_argument("--source-candidate-ledger", type=Path,
                        help="Private source run ledger used to add contact values without putting them in public workflow artifacts")
    args = parser.parse_args()
    try:
        context = json.loads(args.candidate_context.read_text(encoding="utf-8"))
        if args.source_candidate_ledger and args.source_candidate_ledger.is_file():
            with args.source_candidate_ledger.open("r", encoding="utf-8-sig") as source:
                for line in source:
                    if not line.strip():
                        continue
                    candidate = json.loads(line)
                    if candidate.get("candidate_id") != context.get("candidate_id"):
                        continue
                    checks = candidate.get("site_checks") or (candidate.get("evidence") or {}).get("site_checks") or {}
                    contact = checks.get("contact_information") or {}
                    context.update({
                        "email": contact.get("email", ""),
                        "phone": contact.get("phone", ""),
                        "contact_status": contact.get("status", "not_found"),
                        "ContactSource": contact.get("email_source_url", ""),
                        "location": format_location(candidate.get("configured_location", ""), candidate.get("region", "")),
                    })
                    break
        status = json.loads(args.attempt_status.read_text(encoding="utf-8"))
        result = json.loads(args.result.read_text(encoding="utf-8")) if args.result and args.result.is_file() else None
        if not isinstance(context, dict) or not context.get("candidate_id") or not context.get("website_url"):
            raise ValueError("Candidate context must include candidate_id and website_url")
        if not isinstance(status, dict):
            raise ValueError("Attempt status must be a JSON object")
        event = {
            "provider": args.provider,
            "context": context,
            "attempt_status": status,
            "result": result,
            "replicate": args.replicate,
            "source_run_key": args.source_run_key,
            "source_run_id": args.source_run_id,
            "review_run_id": args.review_run_id,
            "actions_run_url": args.actions_run_url,
        }
        row = commit_update(args.private_repo.resolve(), args.branch, event)
        summary = Path(os.environ["GITHUB_STEP_SUMMARY"]) if os.environ.get("GITHUB_STEP_SUMMARY") else None
        replicate_score = row.get(f"replicate_{args.replicate}_score") or "Uncertain"
        running_average = row.get("average_score_exact") or "Uncertain"
        rounded_average = row.get("score") or "Uncertain"
        line = (f"- {args.provider} replicate {args.replicate}/3 raw score: **{replicate_score}**; "
                f"running average: **{running_average}** (rounded **{rounded_average}**; "
                f"{row['provisional_outcome']}; confidence {row['confidence_level']}/3; "
                f"{row['assessment_state']}) — {row['business_name']} ({row['website_key']})\n")
        print(line.strip())
        if summary:
            with summary.open("a", encoding="utf-8") as output:
                output.write(line)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError, RuntimeError) as exc:
        detail = getattr(exc, "stderr", None)
        print(f"score ledger update failed: {detail or exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
