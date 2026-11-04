#!/usr/bin/env python3
"""Validate the two input CSVs and report the planned query and site volume."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def read_column(path: Path, column: str) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or column not in reader.fieldnames:
            raise ValueError(f"{path} must contain a {column!r} column")
        values = [(row.get(column) or "").strip() for row in reader]
    values = [value for value in values if value]
    if not values:
        raise ValueError(f"{path} has no non-empty {column!r} rows")
    if len({value.casefold() for value in values}) != len(values):
        raise ValueError(f"{path} contains duplicate {column!r} values")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--categories-csv", type=Path, default=Path("config/business_categories.csv"))
    parser.add_argument("--cities-csv", type=Path, default=Path("config/cities.csv"))
    parser.add_argument("--max-sites-per-query", type=int, default=8)
    parser.add_argument("--queries-per-job", type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.max_sites_per_query <= 10:
        parser.error("--max-sites-per-query must be between 1 and 10")
    if args.queries_per_job < 1:
        parser.error("--queries-per-job must be positive")
    categories = read_column(args.categories_csv, "category")
    cities = read_column(args.cities_csv, "city")
    queries = len(categories) * len(cities)
    sites = queries * args.max_sites_per_query
    jobs = (queries + args.queries_per_job - 1) // args.queries_per_job
    print(json.dumps({
        "categories": len(categories),
        "cities": len(cities),
        "query_pairs": queries,
        "max_sites_per_query": args.max_sites_per_query,
        "maximum_site_visits": sites,
        "queries_per_job_assumption": args.queries_per_job,
        "jobs_at_that_batch_size": jobs,
        "category_csv": str(args.categories_csv),
        "city_csv": str(args.cities_csv),
        "note": "Planning totals are upper bounds; Duck.ai may return fewer valid unique sites.",
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
