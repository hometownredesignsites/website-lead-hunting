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
        raise ValueError("Provide at least one candidate ID (comma- or newline-separated).")
    if len(values) > MAX_CANDIDATES:
        raise ValueError(f"At most {MAX_CANDIDATES} candidates can be reviewed in one batch.")
    invalid = [value for value in values if not CANDIDATE_ID.fullmatch(value)]
    if invalid:
        raise ValueError(f"Invalid candidate ID(s): {', '.join(invalid)}")
    if len(set(values)) != len(values):
        raise ValueError("Candidate IDs must be unique within a batch.")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-ids", required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    ids = parse_candidate_ids(args.candidate_ids)
    matrix = {"include": [{"candidate_id": candidate_id} for candidate_id in ids]}
    serialized_matrix = json.dumps(matrix, separators=(",", ":"))
    serialized_ids = json.dumps(ids, separators=(",", ":"))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as stream:
            stream.write(f"matrix={serialized_matrix}\n")
            stream.write(f"candidate_ids={serialized_ids}\n")
    print(json.dumps({"matrix": matrix, "candidate_ids": ids}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
