#!/usr/bin/env python3
"""Merge per-candidate visual-review artifacts into a durable candidate ledger."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {path}:{line_number}: {exc}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"Expected a JSON object in {path}:{line_number}")
        records.append(record)
    return records


def artifact_records(results_dir: Path, expected_ids: list[str]) -> dict[str, dict]:
    by_candidate: dict[str, dict] = {}
    for path in results_dir.rglob("review-envelope.json"):
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(envelope, dict):
                raise ValueError("review envelope is not a JSON object")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            candidate_id = next((value for value in expected_ids
                                 if path.parent.name.endswith("-" + value)), None)
            if candidate_id:
                by_candidate[candidate_id] = {
                    "candidate_id": candidate_id,
                    "status": "failed",
                    "error": f"Review envelope could not be read: {exc}",
                    "result": None,
                }
            continue
        candidate_id = envelope.get("candidate_id")
        if not candidate_id:
            continue
        if candidate_id in by_candidate:
            by_candidate[candidate_id] = {
                "candidate_id": candidate_id,
                "status": "failed",
                "error": "More than one review artifact was found for this candidate.",
                "result": None,
            }
            continue
        result_path = path.parent / "website-review" / "review-result.json"
        try:
            result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else None
        except (OSError, json.JSONDecodeError) as exc:
            result = None
            envelope["error"] = f"Review result could not be read: {exc}"
        envelope["result"] = result
        by_candidate[candidate_id] = envelope
    return by_candidate


def merge(ledger_path: Path, results_dir: Path, candidate_ids: list[str],
          source_run_id: str, review_run_id: str) -> dict:
    rows = read_jsonl(ledger_path)
    by_id: dict[str, dict] = {}
    for row in rows:
        candidate_id = row.get("candidate_id")
        if not candidate_id or candidate_id in by_id:
            raise ValueError(f"Candidate ledger has a missing or duplicate candidate_id: {candidate_id}")
        by_id[candidate_id] = row
    unknown = [candidate_id for candidate_id in candidate_ids if candidate_id not in by_id]
    if unknown:
        raise ValueError(f"Candidate ID(s) not present in committed ledger: {', '.join(unknown)}")

    results = artifact_records(results_dir, candidate_ids)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    statuses: dict[str, int] = {}
    for candidate_id in candidate_ids:
        row = by_id[candidate_id]
        envelope = results.get(candidate_id) or {
            "candidate_id": candidate_id,
            "status": "failed",
            "error": "No review artifact was produced for this candidate.",
            "result": None,
        }
        if envelope.get("candidate_id") != candidate_id:
            raise ValueError(f"Review artifact identity mismatch for {candidate_id}")
        result = envelope.get("result") or {}
        valid_result = isinstance(result, dict)
        review = result.get("review") if valid_result else None
        successful = (envelope.get("status") == "passed" and valid_result
                      and result.get("status") == "passed"
                      and isinstance(review, dict) and review.get("candidate_id") == candidate_id)
        status = "passed" if successful else "failed"
        statuses[status] = statuses.get(status, 0) + 1
        attempt = {
            "status": status,
            "candidate_id": candidate_id,
            "reviewed_at": envelope.get("completed_at") or now,
            "source_lead_hunt_run_id": str(source_run_id),
            "review_workflow_run_id": str(review_run_id),
            "error": envelope.get("error") or (result.get("errors") if valid_result else None),
        }
        if successful:
            attempt.update(review)
            attempt["status"] = "passed"
            row["chatgpt_visual_review"] = attempt
        row["chatgpt_visual_review_last_attempt"] = attempt

    ledger_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    return {
        "candidate_ids": candidate_ids,
        "source_lead_hunt_run_id": str(source_run_id),
        "review_workflow_run_id": str(review_run_id),
        "statuses": statuses,
        "successful": statuses.get("passed", 0),
        "failed": statuses.get("failed", 0),
        "updated_at": now,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--candidate-ids", required=True, help="JSON array of requested IDs")
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--review-run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    candidate_ids = json.loads(args.candidate_ids)
    if not isinstance(candidate_ids, list) or not all(isinstance(value, str) for value in candidate_ids):
        raise ValueError("--candidate-ids must be a JSON array of strings")
    summary = merge(args.ledger, args.results_dir, candidate_ids,
                    args.source_run_id, args.review_run_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
