#!/usr/bin/env python3
"""Checkpoint city-upload campaigns across search, visual review, and retries."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from build_visual_review_matrix import select_eligible_candidates
from visual_review_eligibility import is_visual_review_eligible


KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
MAX_CITIES = 20
MAX_VISUAL_BATCH = 20
MAX_LEAD_HUNT_ATTEMPTS = 2
PENDING_RETRY_STATES = {"pending", "ready_to_dispatch", "dispatched"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_rows(path: Path) -> list[dict[str, str]]:
    import csv

    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValueError(f"{path} has no CSV header")
        return [{str(k).strip(): (v or "").strip() for k, v in row.items() if k}
                for row in reader]


def category_values(path: Path) -> list[dict[str, str]]:
    rows = read_rows(path)
    if not rows or "category" not in rows[0]:
        raise ValueError(f"{path} must have a 'category' column")
    values = []
    for index, row in enumerate(rows, 1):
        value = row.get("category", "").strip()
        if not value:
            continue
        # Preserve source-row accounting even if the catalog intentionally
        # repeats a business type under another parent category.
        values.append({"index": len(values) + 1, "business_type": value,
                       "category_group": row.get("Category") or row.get("category_group") or "",
                       "source_row": index})
    if not values:
        raise ValueError(f"{path} has no business types")
    return values


def city_values(path: Path) -> list[str]:
    rows = read_rows(path)
    if not rows or "city" not in rows[0]:
        raise ValueError(f"{path} must have a 'city' column")
    values = [row.get("city", "").strip() for row in rows if row.get("city", "").strip()]
    if not values:
        raise ValueError(f"{path} has no city names")
    if len(values) > MAX_CITIES:
        raise ValueError(f"A city campaign supports at most {MAX_CITIES} cities; received {len(values)}")
    if len({value.casefold() for value in values}) != len(values):
        raise ValueError(f"{path} contains duplicate city names")
    return values


def campaign_dir(workspace: Path, campaign_key: str) -> Path:
    if not KEY_RE.fullmatch(campaign_key):
        raise ValueError("campaign_key contains invalid characters")
    return workspace / "runs" / campaign_key


def state_path(workspace: Path, campaign_key: str) -> Path:
    return campaign_dir(workspace, campaign_key) / "CITY_CAMPAIGN_STATE.json"


def read_state(workspace: Path, campaign_key: str) -> tuple[Path, dict]:
    path = state_path(workspace, campaign_key)
    if not path.is_file():
        raise ValueError(f"No city campaign exists for {campaign_key!r}")
    return path, json.loads(path.read_text(encoding="utf-8-sig"))


def save_state(path: Path, state: dict) -> None:
    state["updated_at"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def all_states(workspace: Path) -> list[tuple[Path, dict]]:
    result = []
    for path in sorted((workspace / "runs").glob("*/CITY_CAMPAIGN_STATE.json")):
        try:
            state = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid city campaign checkpoint {path}: {exc}") from exc
        result.append((path, state))
    return result


def active_campaign_exists(workspace: Path, except_key: str = "") -> bool:
    return any(state.get("status") == "active"
               and state.get("campaign_key") != except_key
               for _, state in all_states(workspace))


def make_category_run_key(campaign_key: str, category_index: int) -> str:
    suffix = f"-bt{category_index:03d}"
    key = campaign_key[:80 - len(suffix)] + suffix
    if not KEY_RE.fullmatch(key):
        raise ValueError("Could not create a valid run key from campaign key")
    return key


def start_next_category(workspace: Path, state: dict) -> None:
    next_index = int(state.get("next_category_index", 1))
    categories = state["categories"]
    if next_index > len(categories):
        state.update({"status": "complete", "stage": "complete", "next_action": None,
                      "completed_at": now_iso()})
        state.setdefault("history", []).append({"event": "campaign_complete", "at": now_iso()})
        return
    current = categories[next_index - 1]
    state.update({
        "status": "active", "stage": "lead_hunt_running",
        "current_category_index": next_index,
        "current_category": current,
        "current_run_key": make_category_run_key(state["campaign_key"], next_index),
        "lead_hunt_attempt": 1,
        "source_lead_hunt_run_id": None,
        "visual_review_batches": 0,
        "next_action": "lead_hunt",
        "pause_reason": None,
    })
    state.setdefault("history", []).append({
        "event": "category_started", "at": now_iso(), "index": next_index,
        "business_type": current["business_type"],
    })


def create_campaign(workspace: Path, campaign_key: str, region: str, categories_csv: Path,
                    cities_csv: Path, max_sites: int = 3,
                    category_index: int = 0) -> tuple[Path, dict]:
    path = state_path(workspace, campaign_key)
    if path.exists():
        raise ValueError(f"Campaign {campaign_key!r} already exists")
    if not 1 <= max_sites <= 10:
        raise ValueError("max_sites must be between 1 and 10")
    categories = category_values(categories_csv)
    catalog_category_count = len(categories)
    if category_index:
        if not 1 <= category_index <= catalog_category_count:
            raise ValueError(f"category_index must be between 1 and {catalog_category_count}")
        selected = categories[category_index - 1].copy()
        selected["index"] = 1
        categories = [selected]
    cities = city_values(cities_csv)
    input_dir = path.parent / "inputs"
    input_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(categories_csv, input_dir / "business_categories.csv")
    shutil.copy2(cities_csv, input_dir / "cities.csv")
    state = {
        "schema_version": 1,
        "campaign_key": campaign_key,
        "status": "queued" if active_campaign_exists(workspace) else "active",
        "stage": "queued",
        "region": region.strip() or "Indiana",
        "categories": categories,
        "catalog_category_count": catalog_category_count,
        "selected_source_category_index": category_index or None,
        "city_count": len(cities),
        "city_names": cities,
        "total_searches": len(categories) * len(cities),
        "max_sites_per_query": max_sites,
        "categories_sha256": hashlib.sha256(categories_csv.read_bytes()).hexdigest(),
        "cities_sha256": hashlib.sha256(cities_csv.read_bytes()).hexdigest(),
        "source_city_commit": os.environ.get("GITHUB_SHA", ""),
        "source_branch": os.environ.get("GITHUB_REF_NAME", "git-chatgpt"),
        "next_category_index": 1,
        "completed_categories": [],
        "processed_callback_run_ids": [],
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "history": [{"event": "campaign_created", "at": now_iso(),
                     "category_count": len(categories), "catalog_category_count": catalog_category_count,
                     "selected_source_category_index": category_index or None,
                     "city_count": len(cities)}],
        "next_action": None,
        "pause_reason": None,
    }
    if state["status"] == "active":
        start_next_category(workspace, state)
    else:
        state["history"].append({"event": "campaign_queued", "at": now_iso()})
    save_state(path, state)
    return path, state


def dispatch_review(state: dict) -> None:
    state["stage"] = "visual_review_running"
    state["next_action"] = "visual_review"
    state["visual_review_batches"] = int(state.get("visual_review_batches", 0)) + 1


def retry_queue_pending(run_dir: Path) -> bool:
    path = run_dir / "CHATGPT_VISUAL_REVIEW_RETRY_QUEUE.json"
    if not path.is_file():
        return False
    queue = json.loads(path.read_text(encoding="utf-8-sig"))
    return any(isinstance(entry, dict) and entry.get("status") in PENDING_RETRY_STATES
               for entry in queue.get("entries", []))


def review_counts(run_dir: Path) -> dict:
    ledger = run_dir / "CANDIDATE_LEDGER.jsonl"
    counts = {"passed": 0, "failed": 0, "retry_needed": 0, "not_reviewed": 0}
    if not ledger.is_file():
        return counts
    for line in ledger.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("site_check_status") != "loaded" or not is_visual_review_eligible(row):
            continue
        last = row.get("chatgpt_visual_review_last_attempt") or {}
        status = last.get("status")
        if status == "passed":
            counts["passed"] += 1
        elif status == "failed":
            counts["failed"] += 1
        elif status == "retry-needed":
            queue_path = run_dir / "CHATGPT_VISUAL_REVIEW_RETRY_QUEUE.json"
            entry_map = {}
            if queue_path.is_file():
                queue = json.loads(queue_path.read_text(encoding="utf-8-sig"))
                entry_map = {entry.get("candidate_id"): entry for entry in queue.get("entries", [])
                             if isinstance(entry, dict)}
            entry = entry_map.get(row.get("candidate_id"), {})
            if entry.get("status") in {"exhausted", "source_artifact_unavailable", "failed", "superseded", "invalid_schedule"}:
                counts["failed"] += 1
            else:
                counts["retry_needed"] += 1
        else:
            counts["not_reviewed"] += 1
    return counts


def continue_after_reviews(workspace: Path, state: dict) -> None:
    run_dir = workspace / "runs" / state["current_run_key"]
    if retry_queue_pending(run_dir):
        state.update({"stage": "waiting_visual_retries", "next_action": None})
        state.setdefault("history", []).append({"event": "waiting_for_visual_retries", "at": now_iso()})
        return
    ledger_path = run_dir / "CANDIDATE_LEDGER.jsonl"
    eligible, total_eligible = (select_eligible_candidates(run_dir, MAX_VISUAL_BATCH)
                                if ledger_path.is_file() else ([], 0))
    if total_eligible:
        dispatch_review(state)
        state["history"].append({"event": "visual_batch_queued", "at": now_iso(),
                                 "selected": len(eligible), "remaining": total_eligible})
        return

    counts = review_counts(run_dir)
    category = state["current_category"]
    state.setdefault("completed_categories", []).append({
        "index": state["current_category_index"],
        "business_type": category["business_type"],
        "category_group": category.get("category_group", ""),
        "run_key": state["current_run_key"],
        "source_lead_hunt_run_id": state.get("source_lead_hunt_run_id"),
        "visual_review": counts,
        "completed_at": now_iso(),
    })
    state["next_category_index"] = int(state["current_category_index"]) + 1
    start_next_category(workspace, state)


def dispatch_payload(state: dict, ref: str) -> tuple[str, dict]:
    action = state.get("next_action")
    if action == "lead_hunt":
        index = int(state["current_category_index"])
        source_row = int(state["current_category"].get("source_row", index))
        # The Actions REST API identifies a workflow by its file name or numeric ID,
        # not by the repository path to the workflow file.
        return "lead-hunt.yml", {
            "ref": ref,
            "inputs": {
                "run_key": state["current_run_key"],
                "region": state["region"],
                "max_queries": str(MAX_CITIES),
                "max_sites_per_query": str(state["max_sites_per_query"]),
                "capture_google_evidence": "false",
                "city_campaign_key": state["campaign_key"],
                "city_campaign_category_index": str(source_row),
            },
        }
    if action == "visual_review":
        return "chatgpt-visual-review-batch.yml", {
            "ref": ref,
            "inputs": {
                "review_run_key": state["current_run_key"],
                "review_source_run_id": str(state["source_lead_hunt_run_id"]),
                "review_candidate_ids": "",
                "automatic_retry_round": "0",
                "retry_request_id": "",
                "city_campaign_key": state["campaign_key"],
            },
        }
    raise ValueError(f"No dispatch action is pending for {state.get('campaign_key')}")


def api_request(token: str, url: str, payload: dict | None = None) -> tuple[int, dict | None]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if payload is not None else "GET",
        headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
                 "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json",
                 "User-Agent": "website-lead-hunting-city-campaign"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", "replace")
        raise RuntimeError(f"GitHub API returned HTTP {exc.code}: {detail}") from exc


def dispatch_state(workspace: Path, campaign_key: str, repository: str,
                   token: str, ref: str) -> bool:
    path, state = read_state(workspace, campaign_key)
    action = state.get("next_action")
    if not action:
        return False
    workflow, payload = dispatch_payload(state, ref)
    url = f"https://api.github.com/repos/{repository}/actions/workflows/{workflow}/dispatches"
    api_request(token, url, payload)
    state["next_action"] = None
    state["last_dispatch"] = {"workflow": workflow, "at": now_iso(), "ref": ref}
    state.setdefault("history", []).append({"event": f"{action}_dispatched", "at": now_iso()})
    save_state(path, state)
    return True


def write_outputs(output: Path | None, campaign_key: str, dispatch: bool, status: str) -> None:
    if not output:
        return
    with output.open("a", encoding="utf-8") as stream:
        stream.write(f"campaign_key={campaign_key}\n")
        stream.write(f"dispatch={'true' if dispatch else 'false'}\n")
        stream.write(f"campaign_status={status}\n")


def promote_queued(workspace: Path) -> tuple[Path, dict] | None:
    if active_campaign_exists(workspace):
        return None
    queued = [(path, state) for path, state in all_states(workspace)
              if state.get("status") == "queued"]
    if not queued:
        return None
    path, state = queued[0]
    state["next_category_index"] = 1
    state["history"].append({"event": "queued_campaign_activated", "at": now_iso()})
    start_next_category(workspace, state)
    save_state(path, state)
    return path, state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True,
                        choices=("start", "resume", "after-lead-hunt", "after-visual-review", "poll", "dispatch"))
    parser.add_argument("--workspace", type=Path, default=Path("."))
    parser.add_argument("--campaign-key", default="")
    parser.add_argument("--region", default="Indiana")
    parser.add_argument("--categories-csv", type=Path, default=Path("config/business_categories.csv"))
    parser.add_argument("--cities-csv", type=Path, default=Path("config/cities.csv"))
    parser.add_argument("--max-sites-per-query", type=int, default=10)
    parser.add_argument("--category-index", type=int, default=0,
                        help="Optional one-based category row; use 0 to run the complete catalog")
    parser.add_argument("--child-run-id", default="")
    parser.add_argument("--child-conclusion", default="")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    parser.add_argument("--ref", default=os.environ.get("GITHUB_REF_NAME", "git-chatgpt"))
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    dispatch_keys: list[str] = []

    if args.mode == "start":
        key = args.campaign_key
        categories = args.categories_csv if args.categories_csv.is_absolute() else workspace / args.categories_csv
        cities = args.cities_csv if args.cities_csv.is_absolute() else workspace / args.cities_csv
        path, state = create_campaign(workspace, key, args.region, categories, cities,
                                     args.max_sites_per_query, args.category_index)
        if state.get("next_action"):
            dispatch_keys.append(key)
    elif args.mode == "resume":
        path, state = read_state(workspace, args.campaign_key)
        if state.get("status") != "paused":
            raise ValueError(f"Campaign is not paused (status={state.get('status')!r})")
        if active_campaign_exists(workspace, args.campaign_key):
            state["status"] = "queued"
            state["stage"] = "queued"
            state["next_action"] = None
        else:
            state["status"] = "active"
            if state.get("stage") == "paused_after_lead_hunt":
                state["stage"] = "lead_hunt_running"
                state["next_action"] = "lead_hunt"
            elif state.get("stage") == "paused_after_visual_review":
                dispatch_review(state)
            else:
                continue_after_reviews(workspace, state)
            if state.get("next_action"):
                dispatch_keys.append(args.campaign_key)
        state["pause_reason"] = None
        state.setdefault("history", []).append({"event": "campaign_resumed", "at": now_iso()})
        save_state(path, state)
    elif args.mode == "after-lead-hunt":
        if not args.child_run_id.isdigit() or not args.child_conclusion:
            raise ValueError("A numeric child run ID and conclusion are required")
        path, state = read_state(workspace, args.campaign_key)
        if args.child_run_id in {str(value) for value in state.get("processed_callback_run_ids", [])}:
            print("Duplicate lead-hunt callback ignored.")
        elif state.get("status") != "active" or state.get("stage") != "lead_hunt_running":
            print("Lead-hunt callback does not match an active campaign stage; ignored.")
        else:
            state.setdefault("processed_callback_run_ids", []).append(args.child_run_id)
            state["processed_callback_run_ids"] = state["processed_callback_run_ids"][-500:]
            run_dir = workspace / "runs" / state["current_run_key"]
            progress_path = run_dir / "LOCATION_CATEGORY_PROGRESS.json"
            progress = json.loads(progress_path.read_text(encoding="utf-8-sig")) if progress_path.is_file() else {}
            state.setdefault("history", []).append({"event": "lead_hunt_completed", "at": now_iso(),
                "run_id": args.child_run_id, "conclusion": args.child_conclusion,
                "progress_status": progress.get("status"), "errors": progress.get("errors", 0)})
            if progress.get("status") == "BLOCKED" or args.child_conclusion == "cancelled":
                state.update({"status": "paused", "stage": "paused_after_lead_hunt",
                              "next_action": None, "pause_reason": "The lead-hunt job was blocked or cancelled; inspect its run and resume."})
            elif (args.child_conclusion == "success" and progress.get("status") == "COMPLETE"
                  and int(progress.get("pending", 0) or 0) == 0
                  and int(progress.get("errors", 0) or 0) == 0):
                state["source_lead_hunt_run_id"] = args.child_run_id
                dispatch_review(state)
            else:
                state.update({"status": "paused", "stage": "paused_after_lead_hunt",
                              "next_action": None,
                              "pause_reason": "Lead-hunt did not complete every city query successfully. Inspect diagnostics before resuming."})
            save_state(path, state)
            if state.get("next_action"):
                dispatch_keys.append(args.campaign_key)
    elif args.mode == "after-visual-review":
        if not args.child_run_id.isdigit() or not args.child_conclusion:
            raise ValueError("A numeric review run ID and conclusion are required")
        path, state = read_state(workspace, args.campaign_key)
        if args.child_run_id in {str(value) for value in state.get("processed_callback_run_ids", [])}:
            print("Duplicate visual-review callback ignored.")
        elif state.get("status") != "active" or state.get("stage") not in {"visual_review_running", "waiting_visual_retries"}:
            print("Visual-review callback does not match an active campaign stage; ignored.")
        else:
            state.setdefault("processed_callback_run_ids", []).append(args.child_run_id)
            state["processed_callback_run_ids"] = state["processed_callback_run_ids"][-500:]
            state.setdefault("history", []).append({"event": "visual_review_batch_completed", "at": now_iso(),
                "run_id": args.child_run_id, "conclusion": args.child_conclusion})
            if args.child_conclusion != "success":
                state.update({"status": "paused", "stage": "paused_after_visual_review",
                              "next_action": None,
                              "pause_reason": "Visual review workflow could not save its batch. Inspect the run before resuming."})
            else:
                continue_after_reviews(workspace, state)
            save_state(path, state)
            if state.get("next_action"):
                dispatch_keys.append(args.campaign_key)
    elif args.mode == "poll":
        for path, state in all_states(workspace):
            if state.get("status") == "active" and state.get("stage") == "waiting_visual_retries":
                continue_after_reviews(workspace, state)
                save_state(path, state)
                if state.get("next_action"):
                    dispatch_keys.append(state["campaign_key"])
        promoted = promote_queued(workspace)
        if promoted:
            p, state = promoted
            if state.get("next_action"):
                dispatch_keys.append(state["campaign_key"])
    elif args.mode == "dispatch":
        if not args.repo or not args.token or not args.ref:
            raise ValueError("GITHUB_REPOSITORY, GITHUB_TOKEN, and a dispatch ref are required")
        if args.campaign_key:
            dispatch_keys = [args.campaign_key]
        else:
            dispatch_keys = [state["campaign_key"] for _, state in all_states(workspace)
                             if state.get("next_action")]
        for key in dispatch_keys:
            dispatch_state(workspace, key, args.repo, args.token, args.ref)
    else:
        raise ValueError(f"Unsupported mode {args.mode}")

    key = args.campaign_key or (dispatch_keys[0] if dispatch_keys else "")
    status = "unknown"
    if key:
        _, result_state = read_state(workspace, key)
        status = result_state.get("status", "unknown")
        print(json.dumps(result_state, ensure_ascii=False, indent=2))
    if args.mode == "poll" and args.github_output:
        args.github_output.write_text("dispatch_keys=" + ",".join(dispatch_keys) + "\n", encoding="utf-8")
    write_outputs(args.github_output if args.mode != "poll" else None, key, bool(dispatch_keys), status)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
