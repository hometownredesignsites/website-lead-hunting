#!/usr/bin/env python3
"""Checkpoint and dispatch bounded, resumable lead-hunt campaigns."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
MAX_QUERIES = 20
MAX_SITES = 10
MAX_RETRIES = 2


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_csv_values(path: Path, column: str) -> list[str]:
    import csv

    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or column not in reader.fieldnames:
            raise ValueError(f"{path} must contain a {column!r} column")
        values = [(row.get(column) or "").strip() for row in reader]
    values = [value for value in values if value]
    if not values:
        raise ValueError(f"{path} has no non-empty {column!r} rows")
    if len({value.casefold() for value in values}) != len(values):
        raise ValueError(f"{path} contains duplicate values in column {column!r}")
    return values


def csv_fingerprint(categories_path: Path, cities_path: Path) -> tuple[int, str, str]:
    categories = read_csv_values(categories_path, "category")
    cities = read_csv_values(cities_path, "city")
    return (len(categories) * len(cities),
            hashlib.sha256(categories_path.read_bytes()).hexdigest(),
            hashlib.sha256(cities_path.read_bytes()).hexdigest())


def state_path(repo: Path, run_key: str) -> Path:
    if not KEY_RE.fullmatch(run_key):
        raise ValueError("run_key must contain only letters, digits, dot, underscore, and hyphen")
    return repo / "runs" / run_key / "CAMPAIGN_STATE.json"


def load_state(repo: Path, run_key: str) -> tuple[Path, dict]:
    path = state_path(repo, run_key)
    if not path.is_file():
        raise ValueError(f"No campaign state exists for run_key {run_key!r}")
    return path, json.loads(path.read_text(encoding="utf-8-sig"))


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now_iso()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_outputs(path: Path | None, state: dict, dispatch: bool) -> None:
    if not path:
        return
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"dispatch={'true' if dispatch else 'false'}\n")
        stream.write(f"campaign_status={state.get('status', '')}\n")
        stream.write(f"run_key={state.get('run_key', '')}\n")


def create_state(repo: Path, run_key: str, region: str, max_queries: int, max_sites: int,
                 capture_google: bool, categories_path: Path, cities_path: Path,
                 max_retries: int = MAX_RETRIES) -> dict:
    path = state_path(repo, run_key)
    if path.exists():
        raise ValueError(f"Campaign state already exists for {run_key!r}; resume it or choose a new run key")
    total, categories_hash, cities_hash = csv_fingerprint(categories_path, cities_path)
    run_manifest = path.parent / "RUN_MANIFEST.json"
    if run_manifest.exists():
        raise ValueError(f"Run key {run_key!r} already has a lead-hunt manifest; choose a new campaign key")
    if not region.strip():
        raise ValueError("region must not be empty")
    if not 1 <= max_queries <= MAX_QUERIES:
        raise ValueError(f"max_queries must be between 1 and {MAX_QUERIES}")
    if not 1 <= max_sites <= MAX_SITES:
        raise ValueError(f"max_sites must be between 1 and {MAX_SITES}")
    if not 0 <= max_retries <= MAX_RETRIES:
        raise ValueError(f"max_retries must be between 0 and {MAX_RETRIES}")
    return {
        "schema_version": 1,
        "run_key": run_key,
        "region": region.strip(),
        "status": "active",
        "total_queries": total,
        "queries_per_batch": max_queries,
        "max_sites_per_query": max_sites,
        "capture_google_evidence": bool(capture_google),
        "max_retries_per_batch": max_retries,
        "categories_sha256": categories_hash,
        "cities_sha256": cities_hash,
        "next_batch_number": 1,
        "pending_batch_number": 1,
        "pending_attempt": 1,
        "completed_batches": 0,
        "total_batch_attempts": 0,
        "last_conclusion": None,
        "last_progress": None,
        "processed_workflow_run_ids": [],
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "history": [{"event": "campaign_started", "at": now_iso()}],
    }


def ensure_inputs_unchanged(state: dict, categories_path: Path, cities_path: Path) -> None:
    total, categories_hash, cities_hash = csv_fingerprint(categories_path, cities_path)
    if (total != state.get("total_queries")
            or categories_hash != state.get("categories_sha256")
            or cities_hash != state.get("cities_sha256")):
        raise ValueError("Campaign CSV inputs changed; use the original CSVs or start a new run_key")


def event_campaign(event: dict, run_details: dict | None = None) -> tuple[str, int, int] | None:
    run = event.get("workflow_run") or {}
    # The workflow_run webhook does not guarantee display_title. The REST
    # workflow-run resource does, so callers should enrich the webhook object.
    if run_details:
        run = {**run, **run_details}
    run_name = str(run.get("name") or "")
    workflow_path = str(run.get("path") or "").split("@", 1)[0]
    if (run.get("event") != "workflow_dispatch"
            or not (run_name == "ChatGPT website lead hunt"
                    or run_name.startswith("ChatGPT website lead hunt "))
            or (workflow_path and workflow_path != ".github/workflows/lead-hunt.yml")):
        return None
    title = str(run.get("display_title") or "")
    match = re.search(r"\bcampaign=([A-Za-z0-9][A-Za-z0-9._-]{0,79})\s+batch=(\d+)\s+attempt=(\d+)\b", title)
    if not match:
        return None
    return match.group(1), int(match.group(2)), int(match.group(3))


def fetch_workflow_run(repo: str, run_id: str, token: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("GITHUB_REPOSITORY must be owner/repository")
    if not str(run_id).isdigit():
        raise ValueError("workflow_run.id must be numeric")
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}"
    request = urllib.request.Request(url, method="GET", headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read(1000).decode("utf-8", "replace")
        raise RuntimeError(f"Could not read completed workflow run: HTTP {exc.code}: {details}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("GitHub returned an invalid workflow-run record")
    return result


def apply_child_completion(state: dict, event: dict, progress: dict | None) -> bool:
    """Record a child batch; return whether another bounded batch should dispatch."""
    identity = event_campaign(event)
    if not identity:
        return False
    run_key, batch_number, attempt = identity
    if run_key != state.get("run_key") or state.get("status") != "active":
        return False
    expected = (int(state.get("pending_batch_number", 0)), int(state.get("pending_attempt", 0)))
    if (batch_number, attempt) != expected:
        return False
    run = event["workflow_run"]
    run_id = str(run.get("id", ""))
    seen = {str(item) for item in state.get("processed_workflow_run_ids", [])}
    if not run_id or run_id in seen:
        return False

    state.setdefault("processed_workflow_run_ids", []).append(run_id)
    state["processed_workflow_run_ids"] = state["processed_workflow_run_ids"][-200:]
    state["total_batch_attempts"] = int(state.get("total_batch_attempts", 0)) + 1
    state["last_conclusion"] = run.get("conclusion")
    state["last_progress"] = progress
    state.setdefault("history", []).append({
        "event": "batch_completed", "at": now_iso(), "batch": batch_number,
        "attempt": attempt, "workflow_run_id": run_id,
        "conclusion": run.get("conclusion"),
        "progress_status": (progress or {}).get("status"),
    })
    state["history"] = state["history"][-200:]

    if run.get("conclusion") == "cancelled":
        state["status"] = "paused"
        state["pause_reason"] = "The lead-hunt batch was cancelled; resume after checking its checkpoint."
        return False

    if (progress or {}).get("status") == "BLOCKED":
        state["status"] = "paused"
        state["pause_reason"] = "Lead-hunt challenge blocked progress; resolve the source condition and resume manually."
        return False

    failed = (run.get("conclusion") != "success" or not progress
              or int(progress.get("errors", 0) or 0) > 0)
    if failed:
        if attempt <= int(state.get("max_retries_per_batch", MAX_RETRIES)):
            state["pending_attempt"] = attempt + 1
            state["pause_reason"] = None
            return True
        state["status"] = "paused"
        state["pause_reason"] = f"Batch {batch_number} failed {attempt} times; resume after inspecting its diagnostics."
        return False

    state["completed_batches"] = int(state.get("completed_batches", 0)) + 1
    state["pause_reason"] = None
    if progress.get("status") == "COMPLETE" and int(progress.get("pending", 0) or 0) == 0:
        state["status"] = "complete"
        state["completed_at"] = now_iso()
        return False

    if int(progress.get("pending", 0) or 0) <= 0:
        state["status"] = "paused"
        state["pause_reason"] = "Progress is not marked complete although no queries are pending; inspect the run summary."
        return False

    state["next_batch_number"] = batch_number + 1
    state["pending_batch_number"] = batch_number + 1
    state["pending_attempt"] = 1
    return True


def dispatch_payload(state: dict, ref: str) -> dict:
    """Build the workflow_dispatch body for exactly one bounded campaign batch."""
    if state.get("status") != "active":
        raise ValueError("Campaign is not active")
    batch_number = int(state.get("pending_batch_number", 0))
    attempt = int(state.get("pending_attempt", 0))
    if batch_number < 1 or attempt < 1:
        raise ValueError("Campaign has no pending batch to dispatch")
    return {
        "ref": ref,
        "inputs": {
            "run_key": state["run_key"],
            "region": state["region"],
            "max_queries": str(state["queries_per_batch"]),
            "max_sites_per_query": str(state["max_sites_per_query"]),
            "capture_google_evidence": "true" if state["capture_google_evidence"] else "false",
            "campaign_key": state["run_key"],
            "campaign_batch": str(batch_number),
            "campaign_attempt": str(attempt),
        },
    }


def dispatch_child(state: dict, repo: str, ref: str, token: str) -> None:
    payload = dispatch_payload(state, ref)
    url = f"https://api.github.com/repos/{repo}/actions/workflows/lead-hunt.yml/dispatches"
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
                 "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status not in {201, 204}:
                raise RuntimeError(f"Workflow dispatch returned unexpected HTTP status {response.status}")
    except urllib.error.HTTPError as exc:
        details = exc.read(1000).decode("utf-8", "replace")
        raise RuntimeError(f"Could not dispatch campaign batch: HTTP {exc.code}: {details}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("start", "resume", "after-run", "dispatch"))
    parser.add_argument("--repo-dir", type=Path, default=Path("."))
    parser.add_argument("--run-key", default="")
    parser.add_argument("--region", default="")
    parser.add_argument("--max-queries", type=int, default=5)
    parser.add_argument("--max-sites-per-query", type=int, default=3)
    parser.add_argument("--max-retries", type=int, default=MAX_RETRIES)
    parser.add_argument("--capture-google-evidence", action="store_true")
    parser.add_argument("--categories-csv", type=Path, default=Path("config/business_categories.csv"))
    parser.add_argument("--cities-csv", type=Path, default=Path("config/cities.csv"))
    parser.add_argument("--child-run-id", default="")
    parser.add_argument("--child-conclusion", default="")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--ref", default="")
    parser.add_argument("--dispatch-to-github", action="store_true")
    args = parser.parse_args()
    repo_dir = args.repo_dir.resolve()
    categories = args.categories_csv if args.categories_csv.is_absolute() else repo_dir / args.categories_csv
    cities = args.cities_csv if args.cities_csv.is_absolute() else repo_dir / args.cities_csv

    if args.mode == "dispatch":
        _, state = load_state(repo_dir, args.run_key)
        if not args.dispatch_to_github:
            raise ValueError("--dispatch-to-github is required in dispatch mode")
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        token = os.environ.get("GITHUB_TOKEN", "")
        if not repo or not token or not args.ref:
            raise ValueError("GITHUB_REPOSITORY, GITHUB_TOKEN, and --ref are required to dispatch")
        dispatch_child(state, repo, args.ref, token)
        print(f"Dispatched campaign {args.run_key}, batch {state['pending_batch_number']}, attempt {state['pending_attempt']}.")
        write_outputs(args.github_output, state, False)
        return 0

    if args.mode == "start":
        path = state_path(repo_dir, args.run_key)
        state = create_state(repo_dir, args.run_key, args.region, args.max_queries,
            args.max_sites_per_query, args.capture_google_evidence, categories, cities, args.max_retries)
        save_state(path, state)
        dispatch = True
    elif args.mode == "resume":
        path, state = load_state(repo_dir, args.run_key)
        ensure_inputs_unchanged(state, categories, cities)
        if state.get("status") != "paused":
            raise ValueError(f"Cannot resume a campaign with status {state.get('status')!r}")
        state["status"] = "active"
        state["pause_reason"] = None
        state["pending_attempt"] = 1
        state.setdefault("history", []).append({"event": "campaign_resumed", "at": now_iso()})
        dispatch = True
        save_state(path, state)
    elif args.mode == "after-run":
        if not args.child_run_id or not args.child_conclusion:
            raise ValueError("--child-run-id and --child-conclusion are required after a lead-hunt batch")
        if not args.repo or not args.token:
            raise ValueError("GITHUB_REPOSITORY and GITHUB_TOKEN are required to resolve campaign metadata")
        run_details = fetch_workflow_run(args.repo, args.child_run_id, args.token)
        identity = event_campaign({"workflow_run": run_details})
        if not identity:
            print("Lead-hunt run is not a campaign batch; no campaign state changed.")
            write_outputs(args.github_output, {"run_key": "", "status": "ignored"}, False)
            return 0
        if args.ref and run_details.get("head_branch") != args.ref:
            raise ValueError("Completed lead-hunt run branch does not match the coordinator branch")
        if args.child_conclusion not in {"success", "failure", "cancelled", "timed_out", "action_required", "neutral", "skipped", "stale"}:
            raise ValueError("Invalid child workflow conclusion")
        event = {"workflow_run": {**run_details, "conclusion": args.child_conclusion}}
        event_run_key = identity[0]
        if args.run_key and args.run_key != event_run_key:
            raise ValueError("Provided run_key does not match campaign metadata in completed run")
        path, state = load_state(repo_dir, event_run_key)
        ensure_inputs_unchanged(state, categories, cities)
        progress_path = path.parent / "LOCATION_CATEGORY_PROGRESS.json"
        progress = json.loads(progress_path.read_text(encoding="utf-8-sig")) if progress_path.is_file() else None
        dispatch = apply_child_completion(state, event, progress)
        save_state(path, state)
        args.run_key = event_run_key
    else:
        raise ValueError(f"Unsupported mode: {args.mode}")

    print(json.dumps(state, indent=2))
    write_outputs(args.github_output, state, dispatch)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
