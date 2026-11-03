#!/usr/bin/env python3
"""Validate candidate IDs and emit a bounded GitHub Actions matrix."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


CANDIDATE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
MAX_CANDIDATES = 20


def parse_candidate_ids(raw: str) -> list[str]:
    values = [item.strip() for item in re.split(r"[\s,;]+", raw) if item.strip()]
    if not values:
        return []
    if len(values) > MAX_CANDIDATES:
        raise ValueError(f"At most {MAX_CANDIDATES} candidates can be reviewed in one batch.")
    invalid = [value for value in values if not CANDIDATE_ID.fullmatch(value)]
    if invalid:
        raise ValueError(f"Invalid candidate ID(s): {', '.join(invalid)}")
    if len(set(values)) != len(values):
        raise ValueError("Candidate IDs must be unique within a batch.")
    return values


def has_valid_visual_score(row: dict) -> bool:
    review = row.get("chatgpt_visual_review")
    if not isinstance(review, dict) or review.get("status") != "passed":
        return False
    candidate_id = row.get("candidate_id")
    return (
        review.get("candidate_id") == candidate_id
        and isinstance(review.get("visual_opportunity_score"), (int, float))
        and review.get("visual_verdict") in {"high", "medium", "pass"}
        and isinstance(review.get("confidence"), str)
        and any(isinstance(review.get(key), list) and review[key]
                for key in ("major_issues", "desktop_top_observations", "desktop_observations"))
    )


def select_eligible_candidates(run_dir: Path, limit: int = MAX_CANDIDATES) -> tuple[list[str], int]:
    ledger_path = run_dir / "CANDIDATE_LEDGER.jsonl"
    if not ledger_path.is_file():
        raise ValueError(f"Candidate ledger not found: {ledger_path}")
    eligible = []
    for line_number, line in enumerate(ledger_path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {ledger_path}:{line_number}: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"Expected a JSON object in {ledger_path}:{line_number}")
        candidate_id = row.get("candidate_id")
        if (not isinstance(candidate_id, str) or not CANDIDATE_ID.fullmatch(candidate_id)
                or row.get("site_check_status") != "loaded"
                or row.get("automated_priority") not in {"high", "medium"}
                or has_valid_visual_score(row)):
            continue
        last_attempt = row.get("chatgpt_visual_review_last_attempt") or {}
        # Recoverable and permanent failures have their own durable retry/failure
        # state; fresh auto-selection must not bypass that accounting.
        if last_attempt.get("status") in {"retry-needed", "failed"}:
            continue
        eligible.append(candidate_id)
    return eligible[:limit], len(eligible)


def validate_scheduled_retry(run_dir: Path, candidate_ids: list[str], retry_round: int,
                             retry_request_id: str, source_run_id: str) -> None:
    queue_path = run_dir / "CHATGPT_VISUAL_REVIEW_RETRY_QUEUE.json"
    if not queue_path.is_file():
        raise ValueError("Scheduled retry queue record is missing; refusing an unqueued automatic retry.")
    queue = json.loads(queue_path.read_text(encoding="utf-8-sig"))
    entries = {entry.get("candidate_id"): entry for entry in queue.get("entries", [])
               if isinstance(entry, dict) and entry.get("candidate_id")}
    ledger_path = run_dir / "CANDIDATE_LEDGER.jsonl"
    ledger = {}
    for line in ledger_path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            row = json.loads(line)
            ledger[row.get("candidate_id")] = row
    invalid = []
    for candidate_id in candidate_ids:
        entry = entries.get(candidate_id) or {}
        last_attempt = (ledger.get(candidate_id) or {}).get("chatgpt_visual_review_last_attempt") or {}
        if (entry.get("status") not in {"ready_to_dispatch", "dispatched"}
                or entry.get("dispatch_request_id") != retry_request_id
                or int(entry.get("automatic_retry_round", -1)) != retry_round
                or str(entry.get("source_lead_hunt_run_id")) != str(source_run_id)
                or last_attempt.get("status") != "retry-needed"
                or str(last_attempt.get("source_lead_hunt_run_id")) != str(source_run_id)
                or str(last_attempt.get("review_workflow_run_id")) != str(entry.get("last_review_workflow_run_id"))):
            invalid.append(candidate_id)
    if invalid:
        raise ValueError("Scheduled retry request is stale or not recorded as dispatched for: "
                         + ", ".join(invalid))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-ids", required=True)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--retry-round", type=int, default=0)
    parser.add_argument("--retry-request-id", default="")
    parser.add_argument("--source-run-id", default="")
    args = parser.parse_args()
    ids = parse_candidate_ids(args.candidate_ids)
    eligible_count = None
    if not ids:
        if not args.run_dir:
            raise ValueError("Automatic selection requires --run-dir")
        ids, eligible_count = select_eligible_candidates(args.run_dir)
    if args.retry_request_id:
        if not args.run_dir or args.retry_round < 1:
            raise ValueError("A scheduled retry requires --run-dir and a positive --retry-round")
        validate_scheduled_retry(args.run_dir, ids, args.retry_round,
                                 args.retry_request_id, args.source_run_id)
    matrix = {"include": [{"candidate_id": candidate_id} for candidate_id in ids]}
    serialized_matrix = json.dumps(matrix, separators=(",", ":"))
    serialized_ids = json.dumps(ids, separators=(",", ":"))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as stream:
            stream.write(f"matrix={serialized_matrix}\n")
            stream.write(f"candidate_ids={serialized_ids}\n")
    plan = {"matrix": matrix, "candidate_ids": ids, "selected_count": len(ids),
            "eligible_count": eligible_count,
            "remaining_eligible": max(0, eligible_count - len(ids)) if eligible_count is not None else None}
    if not ids and eligible_count is not None:
        plan["status"] = "no_eligible_candidates"
        plan["message"] = "No eligible candidates; batch complete."
    print(json.dumps(plan, indent=2))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as stream:
            stream.write(f"selected_count={len(ids)}\n")
            stream.write(f"eligible_count={eligible_count if eligible_count is not None else len(ids)}\n")
            stream.write(f"remaining_eligible={max(0, eligible_count - len(ids)) if eligible_count is not None else 0}\n")
            stream.write(f"selection_mode={'automatic' if eligible_count is not None else 'manual'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
