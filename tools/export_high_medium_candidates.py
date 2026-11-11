#!/usr/bin/env python3
"""Export candidates with a high or medium opportunity signal to CSV."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from location_format import format_location
try:
    from .contact_formatting import normalize_phone
except ImportError:
    from contact_formatting import normalize_phone


FIELDS = [
    "candidate_id", "business_name", "category", "location", "website_url",
    "email", "phone", "contact_name", "contact_status", "ContactSource", "PreviewURL",
    "combined_priority", "automated_priority", "automated_score",
    "visual_verdict", "visual_score", "visual_confidence",
    "visual_review_status", "latest_attempt_status", "evidence_summary",
    "major_issues", "recommended_redesign_focus",
]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"Expected JSON object at {path}:{number}")
        records.append(row)
    return records


def export(run_dir: Path, output: Path | None = None) -> dict[str, int]:
    ledger = run_dir / "CANDIDATE_LEDGER.jsonl"
    if not ledger.is_file():
        raise FileNotFoundError(f"Candidate ledger not found: {ledger}")
    queries = {}
    query_ledger = run_dir / "QUERY_LEDGER.jsonl"
    if query_ledger.is_file():
        queries = {str(row.get("query_index")): row for row in read_jsonl(query_ledger)}
    manifest_path = run_dir / "RUN_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig")) if manifest_path.is_file() else {}
    selected = []
    for row in read_jsonl(ledger):
        visual = row.get("chatgpt_visual_review") or {}
        # Keep the last successful score as the durable assessment when a newer
        # attempt needs retry; attempt status is exported separately.
        automated = str(row.get("automated_priority") or "").lower()
        visual_verdict = str(visual.get("visual_verdict") or "").lower()
        priorities = {value for value in (automated, visual_verdict) if value in {"high", "medium"}}
        if not priorities:
            continue
        combined = "high" if "high" in priorities else "medium"
        site_checks = row.get("site_checks") or (row.get("evidence") or {}).get("site_checks") or {}
        contact = site_checks.get("contact_information") or {}
        signals = row.get("signals") or {}
        signal_text = "; ".join(f"{key}: {value}" for key, value in signals.items()
                                 if str(value).lower() not in {"clear", "false", "none", "0", ""})
        check_issues = []
        for check_name, check in site_checks.items():
            if isinstance(check, dict) and check.get("status") in {"issues_found", "partial", "weak_date_clue_only"}:
                check_issues.append(f"{check_name}: {check['status']}")
        evidence_summary = "; ".join(filter(None, [signal_text, "; ".join(check_issues)]))
        latest_attempt = row.get("chatgpt_visual_review_last_attempt") or {}
        query = queries.get(str(row.get("source_query_index")), {})
        categories = manifest.get("categories") or []
        source_location = row.get("configured_location") or query.get("location") or ""
        location = format_location(source_location, row.get("region", ""))
        if not location:
            location = format_location(row.get("region", ""))
        selected.append({
            "candidate_id": row.get("candidate_id", ""),
            "business_name": row.get("business_name", ""),
            "category": row.get("category") or query.get("category") or (categories[0] if len(categories) == 1 else ""),
            "location": location,
            "website_url": row.get("canonical_url") or row.get("final_url", ""),
            "email": contact.get("email", ""),
            "phone": normalize_phone(contact.get("phone", "")),
            "contact_name": contact.get("contact_name", ""),
            "contact_status": contact.get("status", "not_run" if site_checks.get("status") in {None, "unknown", "not_run"} else "not_found"),
            "ContactSource": contact.get("email_source_url", ""),
            "PreviewURL": "",
            "combined_priority": combined,
            "automated_priority": automated,
            "automated_score": row.get("website_opportunity_score", ""),
            "visual_verdict": visual_verdict,
            "visual_score": visual.get("visual_opportunity_score", ""),
            "visual_confidence": visual.get("confidence", ""),
            "visual_review_status": visual.get("status", "pending"),
            "latest_attempt_status": latest_attempt.get("status", "not_started"),
            "evidence_summary": evidence_summary,
            "major_issues": " | ".join(visual.get("major_issues") or []),
            "recommended_redesign_focus": " | ".join(visual.get("recommended_redesign_focus") or []),
        })
    destination = output or run_dir / "HIGH_MEDIUM_CANDIDATES.csv"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(selected)
    return {"high": sum(row["combined_priority"] == "high" for row in selected),
            "medium": sum(row["combined_priority"] == "medium" for row in selected),
            "total": len(selected)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(json.dumps(export(args.run_dir, args.output), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
