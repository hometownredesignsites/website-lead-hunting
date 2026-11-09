#!/usr/bin/env python3
"""Remove raw business contact values from a run copy before public artifact upload.

The unredacted run is committed to the private operations repository first.
This changes only the working copy that will be attached to public Actions.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def redact_candidate(row: dict) -> dict:
    evidence = row.get("evidence") or {}
    check_sets = [row.get("site_checks") or {}, evidence.get("site_checks") or {}]
    seen = set()
    for checks in check_sets:
        if id(checks) in seen:
            continue
        seen.add(id(checks))
        contact = checks.get("contact_information") or {}
        if contact:
            contact["email_candidate_count"] = len(contact.get("email_candidates") or [])
            contact["phone_candidate_count"] = len(contact.get("phone_candidates") or [])
            contact["email"] = ""
            contact["phone"] = ""
            contact.pop("email_candidates", None)
            contact.pop("phone_candidates", None)
        email_signal = checks.get("contact_email_opportunity") or {}
        if isinstance(email_signal.get("emails_found"), list):
            email_signal["emails_found"] = [
                {key: value for key, value in item.items() if key != "email"}
                for item in email_signal["emails_found"] if isinstance(item, dict)
            ]
    for key in ("email", "phone"):
        if key in row:
            row[key] = ""
    return row


def redact_run(run_dir: Path) -> dict[str, int]:
    ledger = run_dir / "CANDIDATE_LEDGER.jsonl"
    candidates = 0
    if ledger.is_file():
        rows = []
        for line in ledger.read_text(encoding="utf-8-sig").splitlines():
            if line.strip():
                rows.append(redact_candidate(json.loads(line)))
                candidates += 1
        temp = ledger.with_suffix(".jsonl.tmp")
        temp.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
                        encoding="utf-8")
        temp.replace(ledger)

    csv_path = run_dir / "HIGH_MEDIUM_CANDIDATES.csv"
    csv_rows = 0
    if csv_path.is_file():
        with csv_path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            fields = list(reader.fieldnames or [])
            rows = list(reader)
        for row in rows:
            for field in ("email", "phone"):
                if field in row:
                    row[field] = ""
        temp = csv_path.with_suffix(".csv.tmp")
        with temp.open("w", encoding="utf-8-sig", newline="") as destination:
            writer = csv.DictWriter(destination, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        temp.replace(csv_path)
        csv_rows = len(rows)
    return {"candidate_records_redacted": candidates, "candidate_csv_rows_redacted": csv_rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    if not args.run_dir.is_dir():
        parser.error(f"run directory does not exist: {args.run_dir}")
    print(json.dumps(redact_run(args.run_dir), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
