#!/usr/bin/env python3
"""Copy durable run data between the public workflow checkout and private repo."""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def copy_tree(source: Path, destination: Path, *, omit_images: bool = False) -> None:
    if not source.exists():
        return
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if path.is_dir():
            continue
        if omit_images and path.suffix.lower() in IMAGE_SUFFIXES:
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], check=check,
                          text=True, capture_output=True)


def copy_in(repo: Path, workspace: Path) -> None:
    if not (repo / ".git").exists():
        raise ValueError(f"Private checkout is not a git repository: {repo}")
    for name in ("config", "runs"):
        target = workspace / name
        if target.exists():
            shutil.rmtree(target)
    copy_tree(repo / "config", workspace / "config")
    copy_tree(repo / "runs", workspace / "runs")


def copy_run_out(repo: Path, workspace: Path, run_key: str) -> None:
    if not run_key or "/" in run_key or "\\" in run_key or run_key in {".", ".."}:
        raise ValueError("Invalid run key")
    source = workspace / "runs" / run_key
    if not source.is_dir():
        raise ValueError(f"Run directory does not exist: {source}")
    copy_tree(source, repo / "runs" / run_key, omit_images=True)


def copy_runs_out(repo: Path, workspace: Path) -> None:
    copy_tree(workspace / "runs", repo / "runs", omit_images=True)


def commit(repo: Path, paths: list[str], message: str) -> bool:
    token = os.environ.get("PRIVATE_RESULTS_TOKEN", "")
    if not token:
        raise RuntimeError("PRIVATE_RESULTS_TOKEN is not set")
    git(repo, "config", "user.name", "github-actions[bot]")
    git(repo, "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    git(repo, "add", "--", *paths)
    staged = git(repo, "diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        print("Private results repository is already up to date.")
        return False
    git(repo, "commit", "-m", message)
    branch = git(repo, "branch", "--show-current").stdout.strip()
    if not branch:
        raise RuntimeError("Private results checkout is detached; refusing to push")
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    push_command = ["git", "-C", str(repo), "-c",
                    f"http.extraheader=AUTHORIZATION: basic {encoded}",
                    "push", "origin", f"HEAD:{branch}"]
    for attempt in range(3):
        pushed = subprocess.run(push_command, check=False, text=True, capture_output=True)
        if pushed.returncode == 0:
            break
        if attempt == 2:
            raise subprocess.CalledProcessError(pushed.returncode, push_command,
                                                output=pushed.stdout, stderr=pushed.stderr)
        print(f"Private results branch advanced during push; rebasing and retrying ({attempt + 1}/2).")
        git(repo, "fetch", "origin", branch)
        try:
            git(repo, "rebase", f"origin/{branch}")
        except subprocess.CalledProcessError:
            git(repo, "rebase", "--abort", check=False)
            raise
    print(f"Committed private results to {branch}.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("copy-in",):
        command = sub.add_parser(name)
        command.add_argument("--private-repo", type=Path, required=True)
        command.add_argument("--workspace", type=Path, default=Path.cwd())
    out = sub.add_parser("commit-run")
    out.add_argument("--private-repo", type=Path, required=True)
    out.add_argument("--workspace", type=Path, default=Path.cwd())
    out.add_argument("--run-key", required=True)
    out.add_argument("--message", required=True)
    all_runs = sub.add_parser("commit-runs")
    all_runs.add_argument("--private-repo", type=Path, required=True)
    all_runs.add_argument("--workspace", type=Path, default=Path.cwd())
    all_runs.add_argument("--message", required=True)
    smoke = sub.add_parser("smoke-test")
    smoke.add_argument("--private-repo", type=Path, required=True)
    smoke.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        repo = args.private_repo.resolve()
        workspace = args.workspace.resolve() if hasattr(args, "workspace") else Path.cwd()
        if args.command == "copy-in":
            copy_in(repo, workspace)
            print("Loaded config and durable run records from private results repository.")
        elif args.command == "commit-run":
            copy_run_out(repo, workspace, args.run_key)
            commit(repo, [f"runs/{args.run_key}"], args.message)
        elif args.command == "commit-runs":
            copy_runs_out(repo, workspace)
            commit(repo, ["runs"], args.message)
        else:
            if not args.run_id.isdigit():
                raise ValueError("run ID must be numeric")
            marker = repo / "runs" / ".private-sync-smoke" / f"{args.run_id}.json"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"github_run_id": args.run_id}) + "\n", encoding="utf-8")
            commit(repo, [f"runs/.private-sync-smoke/{args.run_id}.json"],
                   f"Verify public Actions private-results write: {args.run_id}")
            marker.unlink()
            commit(repo, [f"runs/.private-sync-smoke/{args.run_id}.json"],
                   f"Remove private-results connectivity marker: {args.run_id}")
            print("Private repository read, write, and cleanup verified.")
    except (OSError, ValueError, subprocess.CalledProcessError, RuntimeError) as exc:
        detail = getattr(exc, "stderr", None)
        print(f"private results sync failed: {detail or exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
