#!/usr/bin/env python3
"""Remove temporary run images after all saved candidate screenshots have Hermes reviews."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    tmp.replace(path)


def guard_reviewed(run_dir: Path) -> list[str]:
    candidates = read_jsonl(run_dir / "CANDIDATE_LEDGER.jsonl")
    pending = []
    for row in candidates:
        shots = row.get("screenshots") or {}
        desktop = shots.get("desktop") or {}
        mobile = shots.get("mobile") or {}
        has_saved_image = any(isinstance(item, dict) and item.get("path") for item in (desktop, mobile))
        review = row.get("hermes_visual_review")
        review_status = review.get("status") if isinstance(review, dict) else None
        if has_saved_image and review_status != "assessed":
            pending.append(row.get("domain") or row.get("candidate_id") or "unknown candidate")
    return pending


def run_cleanup(run_key: str, apply: bool) -> int:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", run_key):
        raise ValueError("run-key must contain only letters, digits, dot, underscore, and hyphen")
    run_dir = (REPO_ROOT / "runs" / run_key).resolve()
    if not run_dir.is_relative_to((REPO_ROOT / "runs").resolve()) or not run_dir.is_dir():
        raise FileNotFoundError(f"Run directory does not exist under runs/: {run_dir}")
    pending = guard_reviewed(run_dir)
    if pending:
        raise ValueError("Refusing to purge screenshots before Hermes has reviewed these candidates: " + ", ".join(pending))
    image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    image_files = [path for path in run_dir.rglob("*") if path.is_file() and path.suffix.lower() in image_suffixes]
    if not apply:
        print(f"Dry run: {len(image_files)} image files would be removed from {run_dir}")
        return 0
    for path in image_files:
        path.unlink()
    for directory in sorted((path for path in run_dir.rglob("*") if path.is_dir()),
                            key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest_path = run_dir / "RUN_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig")) if manifest_path.exists() else {}
    manifest["screenshots_purged_at"] = stamp
    manifest["screenshots_purged_count"] = len(image_files)
    manifest["screenshots_purged_scope"] = "all image files under run directory after Hermes visual review"
    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    tmp.replace(manifest_path)
    progress_path = run_dir / "LOCATION_CATEGORY_PROGRESS.json"
    if progress_path.exists():
        progress = json.loads(progress_path.read_text(encoding="utf-8-sig"))
        progress["screenshots_purged_at"] = stamp
        progress["screenshots_purged_count"] = len(image_files)
        tmp = progress_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(progress, indent=2) + "\n", encoding="utf-8")
        tmp.replace(progress_path)
    print(f"Removed {len(image_files)} image files. Review ledgers and text evidence remain in {run_dir}.")
    print("Commit this cleanup to remove files from the current branch tip. Older Git commits still contain previously committed images.")
    return 0


def rewrite_history(run_key: str, confirmation: str) -> int:
    expected = f"hometownredesignsites/website-lead-hunting:{run_key}"
    if confirmation != expected:
        raise ValueError(f"For history rewrite, --confirm must exactly equal: {expected}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", run_key):
        raise ValueError("run-key must contain only letters, digits, dot, underscore, and hyphen")
    run_dir = (REPO_ROOT / "runs" / run_key).resolve()
    if not run_dir.is_relative_to((REPO_ROOT / "runs").resolve()) or not run_dir.is_dir():
        raise FileNotFoundError(f"Run directory does not exist under runs/: {run_dir}")
    if guard_reviewed(run_dir):
        raise ValueError("History rewrite is blocked until every candidate screenshot has a successful Hermes review")
    if any(path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
           for path in run_dir.rglob("*")):
        raise ValueError("Run images still exist on disk; run purge_screenshots.py --apply first")
    status = subprocess.run(["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout.strip()
    if status:
        raise ValueError("Commit the screenshot deletion and metadata update before rewriting history")
    branch = subprocess.run(["git", "branch", "--show-current"], cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout.strip()
    if not branch:
        raise ValueError("History rewrite requires a named current branch")
    origin = subprocess.run(["git", "remote", "get-url", "origin"], cwd=REPO_ROOT,
                            capture_output=True, text=True, check=True).stdout.strip()
    check = subprocess.run(["git", "filter-repo", "--version"], cwd=REPO_ROOT, capture_output=True, text=True)
    if check.returncode:
        raise RuntimeError("git-filter-repo is required; install it with `python -m pip install git-filter-repo`")
    print(f"Rewriting all local history to remove image files from runs/{run_key}/; this replaces the remote branch history.")
    image_pattern = rf"^runs/{re.escape(run_key)}/.*\.(png|jpg|jpeg|webp|gif)$"
    subprocess.run(["git", "filter-repo", "--path-regex", image_pattern, "--invert-paths", "--force"],
                   cwd=REPO_ROOT, check=True)
    remotes = subprocess.run(["git", "remote"], cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout.split()
    if "origin" not in remotes:
        subprocess.run(["git", "remote", "add", "origin", origin], cwd=REPO_ROOT, check=True)
    subprocess.run(["git", "push", "--force-with-lease", "origin", f"HEAD:{branch}"], cwd=REPO_ROOT, check=True)
    print(f"Screenshot blobs for this run were removed from rewritten remote {branch} history.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-key", required=True)
    parser.add_argument("--apply", action="store_true", help="Delete screenshots after the review gate passes")
    parser.add_argument("--rewrite-history", action="store_true", help="Rewrite/push the current branch after image deletion has been committed")
    parser.add_argument("--confirm", default="", help="Exact confirmation: hometownredesignsites/website-lead-hunting:<run-key>")
    args = parser.parse_args()
    try:
        if args.rewrite_history:
            return rewrite_history(args.run_key, args.confirm)
        return run_cleanup(args.run_key, args.apply)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
