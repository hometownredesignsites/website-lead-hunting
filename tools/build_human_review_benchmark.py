#!/usr/bin/env python3
"""Join human decisions to the newest provider scorecards in private ledgers.

The output contains business URLs and human notes. Keep it in the private
operations repository; do not publish it from the public code repository.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from visual_factor_scoring import ASSESSMENT_VERSION


PROVIDERS = ("chatgpt", "duckai")
LABEL_FIELDS = (
    "category_id", "category", "categories", "business", "location", "website",
    "decision", "decision_source", "notes", "reviewed_at_utc",
)


def site_key(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    return (parsed.hostname or "").casefold().removeprefix("www.").rstrip(".")


def read_ledger(path: Path):
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {path}:{line_number}: {exc}") from exc


def review_candidates(candidate: dict, provider: str):
    for key in (f"{provider}_visual_review", f"{provider}_visual_review_last_attempt"):
        review = candidate.get(key)
        if isinstance(review, dict) and review:
            evidence = review.get("review_evidence")
            if isinstance(evidence, dict):
                yield {**evidence, **{field: value for field, value in review.items()
                                     if field != "review_evidence"}}
            else:
                yield review


def review_sort_key(review: dict) -> tuple[str, int]:
    return (str(review.get("reviewed_at") or ""), 1 if review.get("status") == "passed" else 0)


def assessment_fields(candidate: dict | None, provider: str) -> dict:
    prefix = f"{provider}_"
    empty = {prefix + key: "" for key in (
        "score", "verdict", "site_outcome", "validation_status", "confidence", "workflow_run_id", "factor_assessments"
    )}
    if not candidate:
        return {prefix + "status": "not_assessed", prefix + "assessment_version": "", **empty}

    reviews = [review for review in review_candidates(candidate, provider)]
    factor_reviews = [review for review in reviews
                      if (review.get("score_calibration") or {}).get("version") == ASSESSMENT_VERSION]
    if not factor_reviews:
        return {
            prefix + "status": "not_assessed",
            prefix + "assessment_version": "legacy_excluded" if reviews else "",
            **empty,
        }
    chosen = max(factor_reviews, key=review_sort_key)
    status = chosen.get("review_execution_status") or chosen.get("status", "not_assessed")
    if status == "passed":
        status = "completed"
    site_outcome = chosen.get("site_outcome") or chosen.get("visual_verdict", "")
    return {
        prefix + "status": status,
        prefix + "assessment_version": ASSESSMENT_VERSION,
        prefix + "score": chosen.get("visual_opportunity_score", ""),
        prefix + "verdict": site_outcome,
        prefix + "site_outcome": site_outcome,
        prefix + "validation_status": chosen.get("scorecard_validation_status", ""),
        prefix + "confidence": chosen.get("confidence", ""),
        prefix + "workflow_run_id": chosen.get("review_workflow_run_id", ""),
        prefix + "factor_assessments": json.dumps(chosen.get("factor_assessments"), ensure_ascii=False, sort_keys=True)
        if chosen.get("factor_assessments") else "",
    }


def canonical_label(value: str) -> str:
    normalized = (value or "").strip().casefold()
    return {"high": "High", "medium": "Medium", "pass": "Pass"}.get(normalized, "Unknown")


def assign_stratified_split(rows: list[dict]) -> None:
    """Create a stable 20% holdout within each human-decision class."""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(canonical_label(row.get("decision", "")), []).append(row)
    for group in groups.values():
        ordered = sorted(group, key=lambda row: hashlib.sha256(
            (site_key(row.get("website", "")) or row.get("business", "")).encode("utf-8")
        ).hexdigest())
        for index, row in enumerate(ordered):
            row["benchmark_split"] = "holdout" if index % 5 == 0 else "tuning"


def summarize(rows: list[dict], split: str | None = None) -> dict:
    selected = [row for row in rows if split is None or row.get("benchmark_split") == split]
    report = {"rows": len(selected), "human_decisions": {}, "providers": {}}
    for row in selected:
        label = canonical_label(row.get("decision", ""))
        report["human_decisions"][label] = report["human_decisions"].get(label, 0) + 1
    for provider in PROVIDERS:
        matrix: dict[str, dict[str, int]] = {}
        status_counts: dict[str, int] = {}
        actual_counts: dict[str, int] = {}
        predicted_counts: dict[str, int] = {}
        for row in selected:
            status = row.get(f"{provider}_status", "not_assessed") or "not_assessed"
            status_counts[status] = status_counts.get(status, 0) + 1
            label = canonical_label(row.get("decision", ""))
            verdict = canonical_label(row.get(f"{provider}_verdict", ""))
            if status in {"completed", "passed"} and verdict in {"High", "Medium", "Pass"}:
                row_counts = matrix.setdefault(label, {})
                row_counts[verdict] = row_counts.get(verdict, 0) + 1
                actual_counts[label] = actual_counts.get(label, 0) + 1
                predicted_counts[verdict] = predicted_counts.get(verdict, 0) + 1
        label_metrics = {}
        for label in ("High", "Medium", "Pass"):
            true_positive = matrix.get(label, {}).get(label, 0)
            label_metrics[label] = {
                "true_positive": true_positive,
                "false_negative": actual_counts.get(label, 0) - true_positive,
                "false_positive": predicted_counts.get(label, 0) - true_positive,
                "precision": true_positive / predicted_counts[label] if predicted_counts.get(label) else None,
                "recall": true_positive / actual_counts[label] if actual_counts.get(label) else None,
                "evaluated_human_examples": actual_counts.get(label, 0),
            }
        report["providers"][provider] = {
            "status_counts": status_counts,
            "human_to_model_verdict_counts": matrix,
            "label_metrics": label_metrics,
        }
    paired = [abs(float(row["chatgpt_score"]) - float(row["duckai_score"])) <= 10
              for row in selected
              if row.get("chatgpt_status") in {"completed", "passed"} and row.get("duckai_status") in {"completed", "passed"}
              and isinstance(row.get("chatgpt_score"), (int, float))
              and isinstance(row.get("duckai_score"), (int, float))]
    report["provider_score_agreement"] = {
        "paired_scored_sites": len(paired),
        "within_10_points": sum(paired),
        "within_10_rate": sum(paired) / len(paired) if paired else None,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--human-csv", type=Path, required=True,
                        help="CSV with website and interactive decision columns")
    parser.add_argument("--candidate-ledger", type=Path, action="append", default=[],
                        help="Provider candidate ledger; repeat to join both branches or runs")
    parser.add_argument("--output", type=Path, required=True,
                        help="Output CSV path, normally inside the private operations repository")
    args = parser.parse_args()

    with args.human_csv.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = set(reader.fieldnames or [])
        missing = {"website", "decision"} - headers
        if missing:
            raise ValueError(f"Human CSV is missing required columns: {', '.join(sorted(missing))}")
        labels = list(reader)

    candidates: dict[str, dict] = {}
    for ledger_path in args.candidate_ledger:
        for row in read_ledger(ledger_path):
            key = site_key(row.get("domain") or row.get("final_url") or row.get("canonical_url"))
            if not key:
                continue
            existing = candidates.setdefault(key, {"chatgpt": [], "duckai": [], "candidate_ids": []})
            if row.get("candidate_id"):
                existing["candidate_ids"].append(row["candidate_id"])
            for provider in PROVIDERS:
                existing[provider].extend(review_candidates(row, provider))

    rows = []
    for label in labels:
        key = site_key(label.get("website", ""))
        match = candidates.get(key)
        output = {field: label.get(field, "") for field in LABEL_FIELDS}
        output.update({
            "match_status": "matched" if match else "not_assessed",
            "candidate_ids": " | ".join(dict.fromkeys(match["candidate_ids"])) if match else "",
        })
        for provider in PROVIDERS:
            if match:
                reviews = match[provider]
                factor_reviews = [review for review in reviews
                                  if (review.get("score_calibration") or {}).get("version") == ASSESSMENT_VERSION]
                latest = max(factor_reviews or reviews, key=review_sort_key, default={})
                provider_candidate = {f"{provider}_visual_review": latest}
                output.update(assessment_fields(provider_candidate, provider))
            else:
                output.update(assessment_fields(None, provider))
        rows.append(output)

    assign_stratified_split(rows)

    fields = [*LABEL_FIELDS, "match_status", "benchmark_split", "candidate_ids"]
    for provider in PROVIDERS:
        fields.extend(f"{provider}_{field}" for field in (
            "status", "validation_status", "assessment_version", "score", "verdict", "site_outcome", "confidence", "workflow_run_id", "factor_assessments"
        ))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    summary_path = args.output.with_suffix(".summary.json")
    summary = {
        "output": str(args.output),
        "holdout_method": "stable host-hash order; one of every five rows per human-decision class",
        "overall": summarize(rows),
        "tuning": summarize(rows, "tuning"),
        "holdout": summarize(rows, "holdout"),
        "warning": "Do not tune using holdout rows. Missing assessments are coverage gaps, not negative outcomes.",
    }
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    counts = {}
    for row in rows:
        label = row.get("decision", "unknown").strip().casefold() or "unknown"
        counts[label] = counts.get(label, 0) + 1
    summary = {
        "output": str(args.output),
        "summary": str(summary_path),
        "human_rows": len(rows),
        "human_decisions": counts,
        "matched_hosts": sum(row["match_status"] == "matched" for row in rows),
        "unmatched_hosts": sum(row["match_status"] != "matched" for row in rows),
        "note": "Use the interactive decision column as the label; email and phone columns are intentionally excluded.",
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
