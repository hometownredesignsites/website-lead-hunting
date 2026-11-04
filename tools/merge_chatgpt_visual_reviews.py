#!/usr/bin/env python3
"""Merge per-candidate visual-review artifacts into a durable candidate ledger."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
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
          source_run_id: str, review_run_id: str,
          automatic_retry_round: int = 0, retry_request_id: str = "",
          retry_delay_minutes: int = 30, max_automatic_retry_rounds: int = 2,
          city_campaign_key: str = "") -> dict:
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
    for candidate_id in candidate_ids:
        row = by_id[candidate_id]
        envelope = results.get(candidate_id) or {
            "candidate_id": candidate_id,
            "status": "retry-needed",
            "failure_class": "review_artifact_missing_or_run_interrupted",
            "error": "No review artifact was produced; the candidate may have been interrupted and needs another review run.",
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
        status = "passed" if successful else envelope.get("status")
        if status not in {"failed", "retry-needed"}:
            status = "failed"
        if not successful and isinstance(result, dict) and result.get("status") == "failed":
            errors = result.get("errors") or []
            joined_errors = "; ".join(str(error) for error in errors)
            if "candidate_id does not match" in joined_errors or "website_url does not match" in joined_errors:
                status = "failed"
            elif not envelope.get("error"):
                envelope["error"] = joined_errors or "Review scorecard failed validation."
        attempt = {
            "status": status,
            "candidate_id": candidate_id,
            "reviewed_at": envelope.get("completed_at") or now,
            "source_lead_hunt_run_id": str(source_run_id),
            "review_workflow_run_id": str(review_run_id),
            "automatic_retry_round": automatic_retry_round,
            "retry_request_id": retry_request_id or None,
            "failure_class": envelope.get("failure_class"),
            "error": envelope.get("error") or (result.get("errors") if valid_result else None),
        }
        if isinstance(envelope.get("attempts"), list):
            attempt["attempts"] = envelope["attempts"]
        if isinstance(result, dict) and isinstance(result.get("review"), dict):
            attempt["review_evidence"] = result["review"]
        if successful:
            attempt.update(review)
            attempt["status"] = "passed"
            row["chatgpt_visual_review"] = attempt
        row["chatgpt_visual_review_last_attempt"] = attempt

    ledger_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    queue_path = ledger_path.parent / "CHATGPT_VISUAL_REVIEW_RETRY_QUEUE.json"
    if queue_path.is_file():
        try:
            retry_queue = json.loads(queue_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Retry queue could not be read: {exc}") from exc
    else:
        retry_queue = {"schema_version": 1, "run_key": ledger_path.parent.name, "entries": []}
    if not isinstance(retry_queue, dict) or not isinstance(retry_queue.get("entries"), list):
        raise ValueError("Retry queue must be a JSON object with an entries array")
    entries = {entry.get("candidate_id"): entry for entry in retry_queue["entries"]
               if isinstance(entry, dict) and entry.get("candidate_id")}
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    for candidate_id in candidate_ids:
        row = by_id[candidate_id]
        last_attempt = row.get("chatgpt_visual_review_last_attempt") or {}
        status = last_attempt.get("status")
        entry = entries.get(candidate_id, {"candidate_id": candidate_id})
        entry.update({
            "run_key": ledger_path.parent.name,
            "source_lead_hunt_run_id": str(source_run_id),
            "last_review_workflow_run_id": str(review_run_id),
            "last_status": status,
            "updated_at": now,
        })
        if city_campaign_key:
            entry["city_campaign_key"] = city_campaign_key
        if status == "passed":
            entry.update({"status": "resolved", "not_before": None,
                          "resolved_at": now, "last_dispatch_error": None})
        elif status == "failed":
            entry.update({"status": "failed", "not_before": None,
                          "last_dispatch_error": None})
        elif status == "retry-needed":
            if automatic_retry_round < max_automatic_retry_rounds:
                retry_round = automatic_retry_round + 1
                request_key = f"{source_run_id}:{review_run_id}:{candidate_id}:{retry_round}"
                request_id = hashlib.sha256(request_key.encode("utf-8")).hexdigest()[:24]
                # Repeated merge execution for the same completed workflow must
                # preserve the original due time and request ID.
                same_request = entry.get("retry_request_id") == request_id
                entry.update({
                    "status": entry.get("status") if same_request and entry.get("status") in {"pending", "dispatched"} else "pending",
                    "automatic_retry_round": retry_round,
                    "retry_request_id": request_id,
                    "not_before": entry.get("not_before") if same_request else
                        (now_dt + timedelta(minutes=retry_delay_minutes)).isoformat(timespec="seconds"),
                    "last_dispatch_error": None if not same_request else entry.get("last_dispatch_error"),
                })
            else:
                entry.update({"status": "exhausted", "not_before": None,
                              "automatic_retry_round": automatic_retry_round})
        entries[candidate_id] = entry
    retry_queue.update({"schema_version": 1, "run_key": ledger_path.parent.name,
                        "updated_at": now, "retry_delay_minutes": retry_delay_minutes,
                        "max_automatic_retry_rounds": max_automatic_retry_rounds,
                        "entries": sorted(entries.values(), key=lambda item: item["candidate_id"])})
    queue_path.write_text(json.dumps(retry_queue, indent=2) + "\n", encoding="utf-8")
    # Re-read the durable records after writing. The summary must reflect the
    # candidate ledger that downstream users see, even if envelope metadata
    # was incomplete or conflicted with an existing accepted scorecard.
    statuses: dict[str, int] = {}
    candidate_ids_by_status: dict[str, list[str]] = {"passed": [], "retry-needed": [], "failed": []}
    for candidate_id in candidate_ids:
        durable_attempt = by_id[candidate_id].get("chatgpt_visual_review_last_attempt") or {}
        status = durable_attempt.get("status", "retry-needed")
        if status not in {"passed", "failed", "retry-needed"}:
            status = "failed"
        statuses[status] = statuses.get(status, 0) + 1
        candidate_ids_by_status[status].append(candidate_id)
    return {
        "candidate_ids": candidate_ids,
        "candidate_ids_by_status": candidate_ids_by_status,
        "total": len(candidate_ids),
        "source_lead_hunt_run_id": str(source_run_id),
        "review_workflow_run_id": str(review_run_id),
        "statuses": statuses,
        "successful": statuses.get("passed", 0),
        "retry_needed": statuses.get("retry-needed", 0),
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
    parser.add_argument("--automatic-retry-round", type=int, default=0)
    parser.add_argument("--retry-request-id", default="")
    parser.add_argument("--city-campaign-key", default="")
    parser.add_argument("--retry-delay-minutes", type=int, default=30)
    parser.add_argument("--max-automatic-retry-rounds", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    candidate_ids = json.loads(args.candidate_ids)
    if not isinstance(candidate_ids, list) or not all(isinstance(value, str) for value in candidate_ids):
        raise ValueError("--candidate-ids must be a JSON array of strings")
    if not 0 <= args.automatic_retry_round <= args.max_automatic_retry_rounds:
        raise ValueError("automatic retry round is outside the configured retry limit")
    if args.retry_delay_minutes < 1 or args.max_automatic_retry_rounds < 0:
        raise ValueError("retry delay must be positive and retry limit cannot be negative")
    summary = merge(args.ledger, args.results_dir, candidate_ids,
                    args.source_run_id, args.review_run_id,
                    args.automatic_retry_round, args.retry_request_id,
                    args.retry_delay_minutes, args.max_automatic_retry_rounds,
                    args.city_campaign_key)
    summary["automatic_retry_round"] = args.automatic_retry_round
    summary["retry_queue_file"] = str(args.ledger.parent / "CHATGPT_VISUAL_REVIEW_RETRY_QUEUE.json")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    # Candidate-level review failures are durable outcomes, not merge errors.
    # The workflow should succeed when it records them alongside successful scores.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
