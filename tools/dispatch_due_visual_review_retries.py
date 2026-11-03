#!/usr/bin/env python3
"""Dispatch due, recoverable ChatGPT visual reviews from durable run queues."""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


API_ROOT = "https://api.github.com"
WORKFLOW_FILE = "chatgpt-visual-review-batch.yml"
MAX_CANDIDATES_PER_DISPATCH = 20


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat(timespec="seconds")


def api_request(token: str, url: str, payload: dict | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method="POST" if payload is not None else "GET",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "website-lead-hunting-retry-dispatcher",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        return response.status, json.loads(body) if body else None


def read_candidates(ledger_path: Path) -> dict[str, dict]:
    rows = {}
    with ledger_path.open(encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or not row.get("candidate_id"):
                raise ValueError(f"Invalid candidate record at {ledger_path}:{line_number}")
            rows[row["candidate_id"]] = row
    return rows


def artifact_is_available(token: str, repository: str, run_key: str, source_run_id: str) -> bool:
    expected_name = f"lead-hunt-{run_key}-{source_run_id}"
    base_url = f"{API_ROOT}/repos/{repository}/actions/runs/{source_run_id}/artifacts"
    page = 1
    while True:
        query = urllib.parse.urlencode({"per_page": 100, "page": page})
        _, response = api_request(token, f"{base_url}?{query}")
        for artifact in response.get("artifacts", []):
            if artifact.get("name") == expected_name and not artifact.get("expired"):
                return True
        if page * 100 >= response.get("total_count", 0):
            return False
        page += 1


def dispatch_group(token: str, repository: str, ref: str, group: dict) -> None:
    endpoint = f"{API_ROOT}/repos/{repository}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    payload = {
        "ref": ref,
        "inputs": {
            "review_run_key": group["run_key"],
            "review_source_run_id": str(group["source_lead_hunt_run_id"]),
            "review_candidate_ids": ",".join(group["candidate_ids"]),
            "automatic_retry_round": str(group["automatic_retry_round"]),
            "retry_request_id": group["retry_request_id"],
        },
    }
    status, _ = api_request(token, endpoint, payload)
    if status != 204:
        raise RuntimeError(f"GitHub workflow dispatch returned HTTP {status}")


def process(workspace: Path, repository: str, token: str, ref: str, *, dispatch_ready: bool = False) -> dict:
    now = utcnow()
    due_groups: dict[tuple, list[tuple[Path, dict]]] = {}
    changed_queues: dict[Path, dict] = {}
    queue_paths = sorted(workspace.glob("runs/*/CHATGPT_VISUAL_REVIEW_RETRY_QUEUE.json"))

    for queue_path in queue_paths:
        try:
            queue = json.loads(queue_path.read_text(encoding="utf-8-sig"))
            ledger_path = queue_path.parent / "CANDIDATE_LEDGER.jsonl"
            if not ledger_path.is_file():
                print(f"Skipping {queue_path.parent.name}: candidate ledger is missing.")
                continue
            candidates = read_candidates(ledger_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"Skipping invalid retry queue {queue_path}: {str(exc)[:250]}")
            continue
        queue_entries = queue.get("entries", []) if isinstance(queue, dict) else []
        if not isinstance(queue_entries, list):
            print(f"Skipping {queue_path}: entries must be an array.")
            continue
        for entry in queue_entries:
            if not isinstance(entry, dict):
                print(f"Skipping malformed entry in {queue_path}.")
                continue
            wanted_status = "ready_to_dispatch" if dispatch_ready else "pending"
            if entry.get("status") != wanted_status:
                continue
            if not dispatch_ready:
                if not entry.get("not_before"):
                    continue
                try:
                    not_before = datetime.fromisoformat(entry["not_before"].replace("Z", "+00:00"))
                except ValueError:
                    entry.update({"status": "invalid_schedule", "not_before": None,
                                  "last_dispatch_error": "Invalid retry due timestamp."})
                    changed_queues[queue_path] = queue
                    continue
                if not_before > now:
                    continue
            row = candidates.get(entry.get("candidate_id"))
            latest = (row or {}).get("chatgpt_visual_review_last_attempt") or {}
            if (not row or latest.get("status") != "retry-needed"
                    or str(latest.get("review_workflow_run_id")) != str(entry.get("last_review_workflow_run_id"))):
                entry.update({"status": "superseded", "not_before": None,
                              "updated_at": iso_now(),
                              "last_dispatch_error": "A newer review attempt replaced this queued retry."})
                changed_queues[queue_path] = queue
                continue
            try:
                artifact_available = artifact_is_available(
                    token, repository, entry["run_key"], str(entry["source_lead_hunt_run_id"]))
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
                print(f"Artifact lookup failed for {entry.get('candidate_id')}: {str(exc)[:250]}")
                continue
            if not artifact_available:
                entry.update({"status": "source_artifact_unavailable", "not_before": None,
                              "updated_at": iso_now(),
                              "last_dispatch_error": "Source lead-hunt artifact is missing or expired; new screenshots are required."})
                changed_queues[queue_path] = queue
                continue
            key = (entry["run_key"], str(entry["source_lead_hunt_run_id"]),
                   int(entry["automatic_retry_round"]),
                   entry.get("dispatch_request_id") if dispatch_ready else None)
            due_groups.setdefault(key, []).append((queue_path, entry))

    processed_groups = 0
    for key, entries in sorted(due_groups.items()):
        run_key, source_run_id, retry_round, existing_request_id = key
        entries.sort(key=lambda item: item[1]["candidate_id"])
        for start in range(0, len(entries), MAX_CANDIDATES_PER_DISPATCH):
            chunk = entries[start:start + MAX_CANDIDATES_PER_DISPATCH]
            candidate_ids = [entry["candidate_id"] for _, entry in chunk]
            request_id = existing_request_id or ",".join(sorted(entry["retry_request_id"] for _, entry in chunk))
            group = {
                "run_key": run_key,
                "source_lead_hunt_run_id": source_run_id,
                "automatic_retry_round": retry_round,
                "candidate_ids": candidate_ids,
                "retry_request_id": request_id,
            }
            if not dispatch_ready:
                stamp = iso_now()
                for queue_path, entry in chunk:
                    entry.update({"status": "ready_to_dispatch", "dispatch_prepared_at": stamp,
                                  "dispatch_request_id": request_id, "updated_at": stamp,
                                  "last_dispatch_error": None})
                    if queue_path not in changed_queues:
                        changed_queues[queue_path] = json.loads(queue_path.read_text(encoding="utf-8-sig"))
                    target = next(item for item in changed_queues[queue_path]["entries"]
                                  if item.get("candidate_id") == entry["candidate_id"])
                    target.update(entry)
                processed_groups += 1
                print(f"Prepared retry round {retry_round} for {run_key}: {', '.join(candidate_ids)}")
                continue

            try:
                dispatch_group(token, repository, ref, group)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError) as exc:
                message = str(exc)[:300]
                for queue_path, entry in chunk:
                    entry.update({"status": "pending", "not_before": iso_now(),
                                  "dispatch_failures": int(entry.get("dispatch_failures", 0)) + 1,
                                  "last_dispatch_error": message, "updated_at": iso_now()})
                    if queue_path not in changed_queues:
                        changed_queues[queue_path] = json.loads(queue_path.read_text(encoding="utf-8-sig"))
                    target = next(item for item in changed_queues[queue_path]["entries"]
                                  if item.get("candidate_id") == entry["candidate_id"])
                    target.update(entry)
                print(f"Dispatch failed for {run_key} round {retry_round}: {message}")
                processed_groups += 1
                continue
            stamp = iso_now()
            for queue_path, entry in chunk:
                entry.update({"status": "dispatched", "dispatched_at": stamp,
                              "updated_at": stamp, "last_dispatch_error": None})
                if queue_path not in changed_queues:
                    changed_queues[queue_path] = json.loads(queue_path.read_text(encoding="utf-8-sig"))
                target = next(item for item in changed_queues[queue_path]["entries"]
                              if item.get("candidate_id") == entry["candidate_id"])
                target.update(entry)
            processed_groups += 1
            print(f"Dispatched retry round {retry_round} for {run_key}: {', '.join(candidate_ids)}")

    for queue_path, queue in changed_queues.items():
        queue["updated_at"] = iso_now()
        queue_path.write_text(json.dumps(queue, indent=2) + "\n", encoding="utf-8")
    return {"queue_files": len(queue_paths), "groups_processed": processed_groups,
            "updated_queue_files": len(changed_queues)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path("."))
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    parser.add_argument("--ref", default=os.environ.get("GITHUB_REF_NAME", ""))
    parser.add_argument("--dispatch-ready", action="store_true",
                        help="Dispatch entries already committed as ready_to_dispatch.")
    args = parser.parse_args()
    if not args.repository or not args.token or not args.ref:
        parser.error("repository, token, and ref are required (or set the matching GitHub Actions environment variables)")
    print(json.dumps(process(args.workspace, args.repository, args.token, args.ref,
                            dispatch_ready=args.dispatch_ready), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
