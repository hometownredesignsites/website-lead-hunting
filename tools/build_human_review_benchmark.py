#!/usr/bin/env python3
"""Join human decisions to the newest provider scorecards in private ledgers.

The output contains business URLs and human notes. Keep it in the private
operations repository; do not publish it from the public code repository.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from urllib.parse import urlsplit


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
            yield review


def review_sort_key(review: dict) -> tuple[str, int]:
    return (str(review.get("reviewed_at") or ""), 1 if review.get("status") == "passed" else 0)


def assessment_fields(candidate: dict | None, provider: str) -> dict:
    prefix = f"{provider}_"
    if not candidate:
        return {prefix + key: "" for key in (
            "status", "score", "verdict", "model_score", "model_verdict", "confidence", "workflow_run_id"
        )}

    reviews = [review for review in review_candidates(candidate, provider)]
    valid = [review for review in reviews if review.get("status") == "passed"
             and isinstance(review.get("visual_opportunity_score"), (int, float))]
    chosen = max(valid or reviews, key=review_sort_key, default={})
    return {
        prefix + "status": chosen.get("status", "not_assessed"),
        prefix + "score": chosen.get("visual_opportunity_score", ""),
        prefix + "verdict": chosen.get("visual_verdict", ""),
        prefix + "model_score": chosen.get("model_reported_visual_opportunity_score", ""),
        prefix + "model_verdict": chosen.get("model_reported_visual_verdict", ""),
        prefix + "confidence": chosen.get("confidence", ""),
        prefix + "workflow_run_id": chosen.get("review_workflow_run_id", ""),
    }


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
                valid = [review for review in reviews if review.get("status") == "passed"
                         and isinstance(review.get("visual_opportunity_score"), (int, float))]
                latest = max(valid or reviews, key=review_sort_key, default={})
                provider_candidate = {f"{provider}_visual_review": latest}
                output.update(assessment_fields(provider_candidate, provider))
            else:
                output.update(assessment_fields(None, provider))
        rows.append(output)

    fields = [*LABEL_FIELDS, "match_status", "candidate_ids"]
    for provider in PROVIDERS:
        fields.extend(f"{provider}_{field}" for field in (
            "status", "score", "verdict", "model_score", "model_verdict", "confidence", "workflow_run_id"
        ))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    counts = {}
    for row in rows:
        label = row.get("decision", "unknown").strip().casefold() or "unknown"
        counts[label] = counts.get(label, 0) + 1
    summary = {
        "output": str(args.output),
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
