#!/usr/bin/env python3
"""Create the one-category/one-city-list CSV inputs for a campaign child run."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-run-dir", type=Path, required=True)
    parser.add_argument("--category-index", type=int, required=True,
                        help="One-based index in the campaign's immutable category snapshot")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    category_source = args.campaign_run_dir / "inputs" / "business_categories.csv"
    city_source = args.campaign_run_dir / "inputs" / "cities.csv"
    if not category_source.is_file() or not city_source.is_file():
        raise ValueError("Campaign input snapshots are missing from the private results checkout")
    with category_source.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fieldnames = reader.fieldnames or []
        exact_matches = [name for name in fieldnames if str(name).strip() == "category"]
        if len(exact_matches) > 1:
            raise ValueError("The campaign business_categories.csv has duplicate 'category' columns")
        if exact_matches:
            # The source file may also contain "Category" for the broader
            # category group. Prefer the exact, lowercase business-type field.
            field = exact_matches[0]
        else:
            normalized_matches = [name for name in fieldnames
                                  if str(name).strip().casefold() == "category"]
            if len(normalized_matches) > 1:
                raise ValueError(
                    "The campaign business_categories.csv has ambiguous category headers; "
                    "include an exact lowercase 'category' business-type column"
                )
            field = normalized_matches[0] if normalized_matches else None
        if not field:
            raise ValueError("The campaign business_categories.csv requires a 'category' column")
        rows = list(reader)
    if not 1 <= args.category_index <= len(rows):
        raise ValueError(f"Category index {args.category_index} is outside 1..{len(rows)}")
    category = (rows[args.category_index - 1].get(field) or "").strip()
    if not category:
        raise ValueError(f"Category row {args.category_index} has no business type")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    categories_out = args.output_dir / "business_categories.csv"
    cities_out = args.output_dir / "cities.csv"
    with categories_out.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["category"])
        writer.writerow([category])
    with city_source.open("r", encoding="utf-8-sig", newline="") as stream:
        city_rows = list(csv.reader(stream))
    if len(city_rows) < 2 or not city_rows[0] or city_rows[0][0].strip().casefold() != "city":
        raise ValueError("Campaign cities.csv must have a 'city' header and at least one city")
    if len(city_rows) > 21:
        raise ValueError("Campaign city snapshot exceeds the 20-city limit")
    with cities_out.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerows(city_rows)
    print(f"Prepared category {args.category_index}: {category}; {len(city_rows) - 1} cities")
    print(f"categories_csv={categories_out}")
    print(f"cities_csv={cities_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
