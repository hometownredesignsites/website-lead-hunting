#!/usr/bin/env python3
"""Capture fresh desktop and mobile screenshots for private human-high calibration targets."""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import socket
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

try:
    from .site_audit import PageObserver, load_lazy_images, run_site_checks
    from .page_flow import inspect_page_flow
except ImportError:
    from site_audit import PageObserver, load_lazy_images, run_site_checks
    from page_flow import inspect_page_flow


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


async def capture(page, url: str, path: Path, width: int, height: int) -> dict:
    await page.set_viewport_size({"width": width, "height": height})
    response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(900)
    try:
        await page.evaluate("""async () => {
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
        await page.screenshot(path=str(path), full_page=True, animations="disabled", timeout=30000)
        mode = "full_page"
    except Exception:
        await page.screenshot(path=str(path), full_page=False, animations="disabled", timeout=15000)
        mode = "viewport_fallback"
    if width <= 500:
        from PIL import Image
        with Image.open(path) as screenshot:
            if screenshot.width > width:
                screenshot.crop((0, 0, width, screenshot.height)).save(path)
    return {"status": "saved", "path": path.resolve().relative_to(Path.cwd().resolve()).as_posix(), "mode": mode,
            "http_status": response.status if response else None,
            "final_url": page.url, "viewport": {"width": width, "height": height},
            "page_flow": await inspect_page_flow(page)}


async def capture_run(args) -> int:
    run_dir = args.run_dir.resolve()
    screenshots_dir = run_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    targets = list(csv.DictReader(args.targets_csv.open(encoding="utf-8-sig", newline="")))
    if args.target_url:
        requested_host = (urlsplit(args.target_url.strip()).hostname or "").lower().removeprefix("www.")
        targets = [row for row in targets
                   if (urlsplit((row.get("website") or "").strip()).hostname or "").lower().removeprefix("www.") == requested_host]
        if len(targets) != 1:
            raise ValueError(f"--target-url must match exactly one private calibration target; matched {len(targets)}")
    if not targets:
        raise ValueError("Calibration target CSV has no rows")
    candidates = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        public_host_cache: dict[str, bool] = {}
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
                      "screenshots": {}, "site_checks": {"status": "not_run"},
                      "visual_page_flow": {}}
            desktop_context = await browser.new_context(ignore_https_errors=False,
                viewport={"width": 1440, "height": 1000})
            mobile_context = await browser.new_context(ignore_https_errors=False,
                viewport={"width": 390, "height": 844}, device_scale_factor=1,
                is_mobile=True, has_touch=True)
            mobile_page = await mobile_context.new_page()
            await mobile_page.add_init_script("""(() => {
          const ensureViewport = () => {
            if (!document.head) return;
            let viewport = document.querySelector('meta[name="viewport"]');
            if (!viewport) {
              viewport = document.createElement('meta');
              viewport.name = 'viewport';
              document.head.appendChild(viewport);
            }
            viewport.content = 'width=device-width, initial-scale=1';
          };
          new MutationObserver(ensureViewport).observe(document, {childList: true, subtree: true});
          ensureViewport();
        })();""")
            desktop_page = await desktop_context.new_page()
            observer = PageObserver(desktop_page)
            try:
                record["screenshots"]["desktop"] = await capture(desktop_page, url, desktop_path, 1440, 1000)
                record["visual_page_flow"]["desktop"] = record["screenshots"]["desktop"]["page_flow"]
                title = await desktop_page.title()
                final_url = desktop_page.url
                final_host = (urlsplit(final_url).hostname or "").lower()
                if final_host and final_host not in public_host_cache:
                    public_host_cache[final_host] = await asyncio.to_thread(public_host, final_host)
                if final_host and not public_host_cache[final_host]:
                    raise ValueError("Final website host is not publicly resolvable")
                image_loading = await load_lazy_images(desktop_page)
                try:
                    record["site_checks"] = await run_site_checks(
                        desktop_page, final_url, observer, public_host_cache, public_host, image_loading)
                except Exception as audit_error:
                    record["site_checks"] = {"status": "unknown",
                        "error": f"{type(audit_error).__name__}: {audit_error}"[:500]}
                record["screenshots"]["mobile"] = await capture(mobile_page, final_url, mobile_path, 390, 844)
                record["visual_page_flow"]["mobile"] = record["screenshots"]["mobile"]["page_flow"]
                hero_by_view = {key: value.get("hero_media", {})
                                for key, value in record["visual_page_flow"].items()}
                statuses = [item.get("status") for item in hero_by_view.values()]
                overall = ("uncertain" if "uncertain" in statuses else
                           "absent" if "absent" in statuses else
                           "present" if "present" in statuses else "uncertain")
                record["hero_media"] = {"status": overall, "trigger_views": [key for key, value in hero_by_view.items()
                    if value.get("status") in {"absent", "uncertain"}], **hero_by_view}
                record.update({"site_check_status": "loaded", "site_title": title,
                               "final_url": final_url, "domain": final_host or host,
                               "human_review": {"decision": "high", "basis": "provided human review cohort"}})
            except Exception as exc:
                record["capture_error"] = f"{type(exc).__name__}: {exc}"[:700]
            finally:
                observer.close()
                await desktop_context.close()
                await mobile_context.close()
            candidates.append(record)
        await browser.close()
    ledger_path = run_dir / "CANDIDATE_LEDGER.jsonl"
    ledger_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in candidates),
                           encoding="utf-8")
    manifest = {"run_key": run_dir.name, "run_type": "human_high_calibration_capture",
                "created_at": now_iso(), "target_count": len(candidates),
                "capture_method": "Playwright Chromium; per-site desktop and mobile full-page screenshots plus bounded desktop script audit",
                "human_labels_are_not_sent_to_visual_model": True}
    (run_dir / "RUN_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"run_key": run_dir.name, "candidate_count": len(candidates),
                      "loaded": sum(row["site_check_status"] == "loaded" for row in candidates),
                      "capture_failed": sum(row["site_check_status"] != "loaded" for row in candidates),
                      "candidate_ids": [row["candidate_id"] for row in candidates]}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets-csv", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--target-url", help="Optional website URL that must match exactly one row in the private targets CSV")
    return asyncio.run(capture_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
