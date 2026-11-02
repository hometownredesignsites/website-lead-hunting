#!/usr/bin/env python3
"""Validate and summarize a saved ChatGPT lead-hunt run."""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


def read_json(path: Path) -> tuple[dict, list[str]]:
    if not path.is_file():
        return {}, [f"missing {path.name}"]
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return (value, []) if isinstance(value, dict) else ({}, [f"{path.name} is not a JSON object"])
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"cannot read {path.name}: {exc}"]


def read_jsonl(path: Path) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    errors: list[str] = []
    if not path.is_file():
        return rows, [f"missing {path.name}"]
    for line_no, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("row is not a JSON object")
            rows.append(row)
        except (json.JSONDecodeError, ValueError) as exc:
            errors.append(f"{path.name}:{line_no}: {exc}")
    return rows, errors


def review_status(row: dict, field: str) -> str:
    value = row.get(field)
    if isinstance(value, dict):
        return str(value.get("status") or "unknown")
    return str(value or "pending")


def audit_run(run_dir: Path) -> dict:
    manifest, manifest_errors = read_json(run_dir / "RUN_MANIFEST.json")
    progress, progress_errors = read_json(run_dir / "LOCATION_CATEGORY_PROGRESS.json")
    queries, query_errors = read_jsonl(run_dir / "QUERY_LEDGER.jsonl")
    candidates, candidate_errors = read_jsonl(run_dir / "CANDIDATE_LEDGER.jsonl")
    errors = manifest_errors + progress_errors + query_errors + candidate_errors
    warnings: list[str] = []
    google_ledger_path = run_dir / "GOOGLE_SEARCH_LEDGER.jsonl"
    google_rows, google_errors = read_jsonl(google_ledger_path) if google_ledger_path.exists() else ([], [])
    errors.extend(google_errors)

    categories = manifest.get("categories") or []
    cities = manifest.get("locations") or []
    expected = int(manifest.get("configured_queries") or len(categories) * len(cities))
    expected_indices = set(range(1, expected + 1))
    query_by_index: dict[int, dict] = {}
    duplicate_indices: list[int] = []
    for row in queries:
        try:
            index = int(row["query_index"])
        except (KeyError, TypeError, ValueError):
            errors.append("query row has no valid query_index")
            continue
        if index in query_by_index:
            duplicate_indices.append(index)
        query_by_index[index] = row
        if categories and cities and 1 <= index <= expected:
            category_index = (index - 1) // len(cities)
            city_index = (index - 1) % len(cities)
            expected_category, expected_city = categories[category_index], cities[city_index]
            if row.get("category") != expected_category or row.get("location") != expected_city:
                errors.append(f"query {index} does not match the configured CSV inputs")
    if duplicate_indices:
        errors.append(f"duplicate query indices: {sorted(set(duplicate_indices))}")
    unexpected = sorted(set(query_by_index) - expected_indices)
    if unexpected:
        errors.append(f"unexpected query indices: {unexpected[:20]}")
    missing = sorted(expected_indices - set(query_by_index))

    query_statuses = Counter(str(row.get("terminal_status") or "unknown") for row in queries)
    google_statuses = Counter(str(row.get("terminal_status") or "unknown") for row in google_rows)
    google_dispositions: Counter[str] = Counter(
        str(result.get("host_screen_disposition") or "unclassified")
        for row in google_rows for result in (row.get("organic_results") or [])
    )
    dispositions: Counter[str] = Counter()
    unaccounted_results = 0
    returned_count = 0
    site_assessed_count = 0
    by_category: dict[str, Counter] = defaultdict(Counter)
    by_city: dict[str, Counter] = defaultdict(Counter)
    per_query_counts: dict[int, Counter] = {}
    for index, row in query_by_index.items():
        results = row.get("organic_results") or []
        actual_returned = len(results)
        returned_count += actual_returned
        if row.get("organic_results_returned_count") not in (None, actual_returned):
            errors.append(f"query {index} returned count disagrees with its results array")
        qcounts = Counter()
        for result in results:
            disposition = str(result.get("result_disposition") or "unaccounted")
            dispositions[disposition] += 1
            qcounts[disposition] += 1
            if disposition == "unaccounted":
                unaccounted_results += 1
            if disposition == "site_assessed":
                site_assessed_count += 1
        category = str(row.get("category") or "unknown")
        city = str(row.get("location") or "unknown")
        by_category[category]["queries"] += 1
        by_category[category]["returned"] += actual_returned
        by_city[city]["queries"] += 1
        by_city[city]["returned"] += actual_returned
        per_query_counts[index] = qcounts
    if unaccounted_results:
        warnings.append(f"{unaccounted_results} returned result(s) have no disposition yet")
    if manifest.get("google_evidence_enabled"):
        google_indices = {int(row["query_index"]) for row in google_rows if row.get("query_index") is not None}
        if len(google_indices) < min(expected, len(queries)):
            warnings.append(f"Google evidence is present for {len(google_indices)} of {min(expected, len(queries))} recorded ChatGPT queries")
    if google_rows:
        for google_row in google_rows:
            query_row = query_by_index.get(int(google_row.get("query_index", 0)))
            if query_row and google_row.get("exact_query") != query_row.get("query"):
                errors.append(f"Google evidence query {google_row.get('query_index')} does not match its ChatGPT query text")

    candidate_domains = [str(row.get("domain") or "").lower() for row in candidates]
    candidate_domains = [domain for domain in candidate_domains if domain]
    duplicate_domains = sorted(domain for domain, count in Counter(candidate_domains).items() if count > 1)
    if duplicate_domains:
        errors.append(f"candidate ledger has duplicate domains: {duplicate_domains[:20]}")

    site_statuses = Counter(str(row.get("site_check_status") or "unknown") for row in candidates)
    human_reviews = Counter(review_status(row, "human_review") for row in candidates)
    hermes_reviews = Counter(review_status(row, "hermes_visual_review") for row in candidates)
    audit_names = ("http_links_and_resources", "broken_images", "contact_form_privacy",
                   "broken_links_and_interactions", "latest_blog_post", "content_freshness",
                   "copyright_footer_signal", "legacy_twitter_branding")
    site_audit_statuses: dict[str, Counter] = {name: Counter() for name in audit_names}
    site_audit_candidates = 0
    legacy_twitter_detected = 0
    old_copyright_footer_detected = 0
    for row in candidates:
        checks = row.get("site_checks") or (row.get("evidence") or {}).get("site_checks") or {}
        if not isinstance(checks, dict) or not checks:
            for name in audit_names:
                site_audit_statuses[name]["unknown"] += 1
            continue
        site_audit_candidates += 1
        for name in audit_names:
            value = checks.get(name)
            status = str((value or {}).get("status") or "unknown") if isinstance(value, dict) else "unknown"
            site_audit_statuses[name][status] += 1
        if (checks.get("legacy_twitter_branding") or {}).get("legacy_twitter_logo_detected"):
            legacy_twitter_detected += 1
        if (checks.get("copyright_footer_signal") or {}).get("older_years"):
            old_copyright_footer_detected += 1
    screenshot_paths = 0
    screenshots_present = 0
    for row in candidates:
        for image in (row.get("screenshots") or {}).values():
            value = image.get("path") if isinstance(image, dict) else image
            if not value:
                continue
            screenshot_paths += 1
            path = Path(value)
            if not path.is_absolute():
                path = run_dir.parent.parent / path if path.parts and path.parts[0] == "runs" else run_dir / path
            if path.is_file():
                screenshots_present += 1
    if screenshots_present < screenshot_paths and not manifest.get("screenshots_purged_at"):
        warnings.append(f"{screenshots_present}/{screenshot_paths} candidate screenshot files are present in this copy of the run")

    expected_status = str(progress.get("status") or "unknown")
    success = query_statuses.get("success", 0)
    complete = (expected > 0 and len(query_by_index) == expected and success == expected and
                not missing and not unaccounted_results)
    if progress.get("recorded_queries") not in (None, len(queries)):
        errors.append("progress recorded_queries disagrees with QUERY_LEDGER.jsonl")
    if progress.get("total_queries") not in (None, expected):
        errors.append("progress total_queries disagrees with RUN_MANIFEST.json")

    summary = {
        "run_key": run_dir.name,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "engine": manifest.get("engine"),
        "input_sources": manifest.get("input_sources", {}),
        "expected_queries": expected,
        "query_rows": len(queries),
        "missing_query_indices": missing,
        "query_statuses": dict(sorted(query_statuses.items())),
        "reported_result_counts_available": any(row.get("organic_results_reported_count") is not None for row in queries),
        "results_returned": returned_count,
        "result_dispositions": dict(sorted(dispositions.items())),
        "results_without_disposition": unaccounted_results,
        "results_site_assessed": site_assessed_count,
        "unique_candidate_domains": len(set(candidate_domains)),
        "candidate_site_statuses": dict(sorted(site_statuses.items())),
        "site_audit_candidates": site_audit_candidates,
        "site_audit_statuses": {name: dict(sorted(counts.items())) for name, counts in site_audit_statuses.items()},
        "legacy_twitter_logo_candidates": legacy_twitter_detected,
        "old_copyright_footer_candidates": old_copyright_footer_detected,
        "human_review_statuses": dict(sorted(human_reviews.items())),
        "hermes_visual_review_statuses": dict(sorted(hermes_reviews.items())),
        "candidate_screenshots_present": screenshots_present,
        "candidate_screenshot_paths": screenshot_paths,
        "screenshots_purged_at": manifest.get("screenshots_purged_at"),
        "screenshots_purged_count": manifest.get("screenshots_purged_count", 0),
        "progress_status": expected_status,
        "google_evidence_rows": len(google_rows),
        "google_evidence_statuses": dict(sorted(google_statuses.items())),
        "google_reported_result_counts_available": any(row.get("organic_results_reported") is not None for row in google_rows),
        "google_results_returned": sum(len(row.get("organic_results") or []) for row in google_rows),
        "google_result_host_screen_dispositions": dict(sorted(google_dispositions.items())),
        "screenshots_commit_enabled": manifest.get("commit_screenshots_enabled"),
        "complete": complete,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "by_category": {name: dict(counts) for name, counts in sorted(by_category.items())},
        "by_city": {name: dict(counts) for name, counts in sorted(by_city.items())},
    }
    return summary


def summary_markdown(summary: dict) -> str:
    lines = [f"# Run summary: {summary['run_key']}", "",
             f"- **Validation:** {'valid' if summary['valid'] else 'invalid'}",
             f"- **Completion:** {'complete' if summary['complete'] else 'in progress'} ({summary['progress_status']})",
             f"- **Queries:** {summary['query_rows']}/{summary['expected_queries']}",
             f"- **Returned candidates:** {summary['results_returned']}",
             f"- **Unique candidate sites:** {summary['unique_candidate_domains']}",
             f"- **Sites directly assessed:** {summary['results_site_assessed']}",
             f"- **Reported search-engine counts available:** {'yes' if summary['reported_result_counts_available'] else 'no (ChatGPT discovery does not expose this count)'}",
             f"- **Google comparison rows:** {summary['google_evidence_rows']}",
             f"- **Google reported-result counts available:** {'yes' if summary['google_reported_result_counts_available'] else 'no'}",
             f"- **Screenshot commits enabled:** {summary['screenshots_commit_enabled']}",
             f"- **Candidate screenshots present in this run copy:** {summary['candidate_screenshots_present']}/{summary['candidate_screenshot_paths']}",
             f"- **Images purged after scoring:** {summary['screenshots_purged_count']} ({summary['screenshots_purged_at'] or 'no'})",
             "", "## Query statuses", "", "| Status | Count |", "|---|---:|"]
    lines.extend(f"| {status} | {count} |" for status, count in summary["query_statuses"].items())
    lines.extend(["", "## Returned-result dispositions", "", "| Disposition | Count |", "|---|---:|"])
    lines.extend(f"| {status} | {count} |" for status, count in summary["result_dispositions"].items())
    if summary["google_evidence_rows"]:
        lines.extend(["", "## Google SERP comparison evidence", "",
                      f"Captured {summary['google_evidence_rows']} raw result pages; {summary['google_results_returned']} organic links extracted; reported result counts available: {'yes' if summary['google_reported_result_counts_available'] else 'no'}. These pages were parsed for comparison but their businesses were not independently visited in the Google pass.", "",
                      "| Google capture status | Count |", "|---|---:|"])
        lines.extend(f"| {status} | {count} |" for status, count in summary["google_evidence_statuses"].items())
    lines.extend(["", "## Candidate review", "", "| Review state | Count |", "|---|---:|"])
    review_states = {f"Human: {key}": value for key, value in summary["human_review_statuses"].items()}
    review_states.update({f"Hermes visual: {key}": value for key, value in summary["hermes_visual_review_statuses"].items()})
    lines.extend(f"| {status} | {count} |" for status, count in sorted(review_states.items()))
    lines.extend(["", "## Automated website checks", "",
                  f"Structured site checks are available for {summary['site_audit_candidates']}/{summary['unique_candidate_domains']} candidates. Findings are evidence; they do not replace human visual review.",
                  "", "| Check | Findings |", "|---|---:|"])
    for name, counts in summary["site_audit_statuses"].items():
        if name == "latest_blog_post":
            finding = f"{counts.get('found', 0)} dated post(s) found; {counts.get('not_found', 0)} not found"
        elif name == "content_freshness":
            finding = (f"{counts.get('dated_content_found', 0)} dated-content clue(s); "
                       f"{counts.get('weak_date_clue_only', 0)} weak-only; {counts.get('unknown', 0)} unknown")
        elif name == "legacy_twitter_branding":
            finding = f"{summary['legacy_twitter_logo_candidates']} possible legacy logo(s)"
        elif name == "copyright_footer_signal":
            finding = (f"{summary['old_copyright_footer_candidates']} possible stale footer(s); "
                       "copyright years do not count as content freshness")
        else:
            finding = (f"{counts.get('issues_found', 0)} issue(s); "
                       f"{counts.get('partial', 0)} partial; {counts.get('unknown', 0)} unknown")
        lines.append(f"| {name} | {finding} |")
    for heading, records in (("Category totals", summary["by_category"]), ("City totals", summary["by_city"])):
        lines.extend(["", f"## {heading}", "", "| Input | Queries recorded | Results returned |", "|---|---:|---:|"])
        lines.extend(f"| {name} | {counts.get('queries', 0)} | {counts.get('returned', 0)} |" for name, counts in records.items())
    for heading, items in (("Errors", summary["errors"]), ("Warnings", summary["warnings"])):
        if items:
            lines.extend(["", f"## {heading}", ""])
            lines.extend(f"- {item}" for item in items)
    lines.extend(["", "Detailed per-query results and source evidence remain in `QUERY_LEDGER.jsonl`; candidate assessments and human decisions remain in `CANDIDATE_LEDGER.jsonl`.", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--write-summary", action="store_true", help="Write RUN_SUMMARY.json and RUN_SUMMARY.md")
    parser.add_argument("--require-complete", action="store_true", help="Return nonzero if expected queries are incomplete")
    args = parser.parse_args()
    run_dir = args.run_dir.expanduser().resolve()
    summary = audit_run(run_dir)
    rendered = json.dumps(summary, indent=2, ensure_ascii=False)
    print(rendered)
    if args.write_summary:
        (run_dir / "RUN_SUMMARY.json").write_text(rendered + "\n", encoding="utf-8")
        (run_dir / "RUN_SUMMARY.md").write_text(summary_markdown(summary), encoding="utf-8")
    if not summary["valid"] or (args.require_complete and not summary["complete"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
