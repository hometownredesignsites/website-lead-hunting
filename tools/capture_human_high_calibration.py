#!/usr/bin/env python3
"""Capture fresh desktop and mobile screenshots for private human-high calibration targets."""
from __future__ import annotations

import argparse
import csv
import json
import re
import socket
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def public_host(host: str) -> bool:
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    return bool(addresses) and all(not (ip := __import__("ipaddress").ip_address(value)).is_private
                                   and not ip.is_loopback and not ip.is_link_local
                                   and not ip.is_multicast and not ip.is_reserved
                                   and not ip.is_unspecified for value in addresses)


def safe_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError(f"Invalid public website URL: {value!r}")
    if not public_host(parts.hostname):
        raise ValueError(f"Website host is not publicly resolvable: {parts.hostname}")
    return value.strip()


def candidate_id(index: int, host: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", host).strip("_").lower()[:100]
    return f"cal-high-{index:03d}-{slug}"


def capture(page, url: str, path: Path, width: int, height: int) -> dict:
    page.set_viewport_size({"width": width, "height": height})
    response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(900)
    try:
        page.evaluate("""async () => {
          const images = Array.from(document.images);
          for (const image of images) image.loading = 'eager';
          await Promise.all(images.map(image => image.decode().catch(() => {})));
          window.scrollTo(0, document.body.scrollHeight);
          await new Promise(resolve => setTimeout(resolve, 250));
          window.scrollTo(0, 0);
          await new Promise(resolve => setTimeout(resolve, 250));
        }""")
    except Exception:
        pass
    try:
        page.screenshot(path=str(path), full_page=True, animations="disabled", timeout=30000)
        mode = "full_page"
    except Exception:
        page.screenshot(path=str(path), full_page=False, animations="disabled", timeout=15000)
        mode = "viewport_fallback"
    return {"status": "saved", "path": path.resolve().relative_to(Path.cwd().resolve()).as_posix(), "mode": mode,
            "http_status": response.status if response else None,
            "final_url": page.url, "viewport": {"width": width, "height": height}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets-csv", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    screenshots_dir = run_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    targets = list(csv.DictReader(args.targets_csv.open(encoding="utf-8-sig", newline="")))
    if not targets:
        raise ValueError("Calibration target CSV has no rows")
    candidates = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        desktop_context = browser.new_context(ignore_https_errors=False, viewport={"width": 1440, "height": 1000})
        mobile_context = browser.new_context(ignore_https_errors=False, viewport={"width": 390, "height": 844},
                                             device_scale_factor=1, is_mobile=True, has_touch=True)
        desktop_page = desktop_context.new_page()
        mobile_page = mobile_context.new_page()
        for index, target in enumerate(targets, 1):
            business = (target.get("business") or "").strip()
            location = (target.get("location") or "").strip()
            url = safe_url(target.get("website") or "")
            if (target.get("human_label") or "").strip().lower() != "high":
                raise ValueError(f"Target row {index} is not labeled human_high")
            host = (urlsplit(url).hostname or "").lower()
            identifier = candidate_id(index, host)
            desktop_path = screenshots_dir / f"{identifier}_desktop.png"
            mobile_path = screenshots_dir / f"{identifier}_mobile.png"
            record = {"candidate_id": identifier, "business_name": business,
                      "category": "Human-reviewed High calibration cohort",
                      "configured_location": location, "region": "Colorado",
                      "domain": host, "canonical_url": url, "final_url": url,
                      "site_title": "", "site_check_status": "error",
                      "screenshots": {}}
            try:
                record["screenshots"]["desktop"] = capture(desktop_page, url, desktop_path, 1440, 1000)
                title = desktop_page.title()
                final_url = desktop_page.url
                final_host = (urlsplit(final_url).hostname or "").lower()
                if final_host and not public_host(final_host):
                    raise ValueError("Final website host is not publicly resolvable")
                record["screenshots"]["mobile"] = capture(mobile_page, final_url, mobile_path, 390, 844)
                record.update({"site_check_status": "loaded", "site_title": title,
                               "final_url": final_url, "domain": final_host or host,
                               "human_review": {"decision": "high", "basis": "provided human review cohort"}})
            except Exception as exc:
                record["capture_error"] = f"{type(exc).__name__}: {exc}"[:700]
            candidates.append(record)
        desktop_context.close()
        mobile_context.close()
        browser.close()
    ledger_path = run_dir / "CANDIDATE_LEDGER.jsonl"
    ledger_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in candidates),
                           encoding="utf-8")
    manifest = {"run_key": run_dir.name, "run_type": "human_high_calibration_capture",
                "created_at": now_iso(), "target_count": len(candidates),
                "capture_method": "Playwright Chromium; one desktop and one mobile full-page screenshot per site",
                "human_labels_are_not_sent_to_visual_model": True}
    (run_dir / "RUN_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"run_key": run_dir.name, "candidate_count": len(candidates),
                      "loaded": sum(row["site_check_status"] == "loaded" for row in candidates),
                      "capture_failed": sum(row["site_check_status"] != "loaded" for row in candidates),
                      "candidate_ids": [row["candidate_id"] for row in candidates]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
