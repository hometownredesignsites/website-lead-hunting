#!/usr/bin/env python3
"""Capture one supplied public website for the standard ChatGPT visual-review workflow."""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

try:
    from .capture_human_high_calibration import capture, public_host, safe_url
    from .lead_hunt import score_signals
    from .page_flow import inspect_page_flow
    from .site_audit import PageObserver, load_lazy_images, run_site_checks
except ImportError:
    from capture_human_high_calibration import capture, public_host, safe_url
    from lead_hunt import score_signals
    from page_flow import inspect_page_flow
    from site_audit import PageObserver, load_lazy_images, run_site_checks


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def compact_slug(value: str, limit: int = 72) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")[:limit] or "site"


async def viewport_metrics(page, final_url: str) -> dict:
    return await page.evaluate("""(finalUrl) => {
      const text = document.body?.innerText || '';
      const copyrightYears = [...text.matchAll(/(?:©|copyright\\s*)\\s*(20\\d{2})/gi)]
        .map(match => Number(match[1]));
      const links = [...document.querySelectorAll('a[href]')];
      const hasContactPath = links.some(a => /contact|get-in-touch|estimate|quote/i.test(
        `${a.innerText || ''} ${a.getAttribute('href') || ''}`));
      const hasCta = [...document.querySelectorAll('a,button,[role="button"]')].some(el =>
        /contact|estimate|quote|get started|free consultation/i.test(el.innerText || el.getAttribute('aria-label') || ''));
      const images = [...document.images];
      return {
        viewportMeta: !!document.querySelector('meta[name="viewport"]'),
        horizontalOverflowPx: Math.max(0, document.documentElement.scrollWidth - innerWidth),
        copyrightYears,
        hasContactPath,
        hasCta,
        titlePresent: !!document.title.trim(),
        descriptionPresent: !!document.querySelector('meta[name="description"]')?.content?.trim(),
        imageCount: images.length,
        loadedImageCount: images.filter(image => image.complete && image.naturalWidth > 0).length,
        https: new URL(finalUrl).protocol === 'https:'
      };
    }""", final_url)


async def capture_one(args: argparse.Namespace) -> dict:
    requested_url = safe_url(args.website_url)
    parts = urlsplit(requested_url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    if not host or not public_host(host):
        raise ValueError("Website host is not publicly resolvable")
    run_dir = args.output_root.resolve() / args.run_key
    screenshots_dir = run_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=False)
    candidate_id = f"direct-{compact_slug(host, 52)}-{args.run_id}"
    desktop_path = screenshots_dir / f"{candidate_id}_desktop.png"
    mobile_path = screenshots_dir / f"{candidate_id}_mobile.png"
    public_hosts: dict[str, bool] = {host: True}

    record = {
        "candidate_id": candidate_id,
        "business_name": args.business_name.strip(),
        "category": args.category.strip() or "Direct URL assessment",
        "configured_location": args.city.strip(),
        "region": args.city.strip(),
        "domain": host,
        "canonical_url": requested_url,
        "final_url": requested_url,
        "site_title": "",
        "site_check_status": "error",
        "screenshots": {},
        "site_checks": {"status": "not_run"},
        "visual_page_flow": {},
        "candidate_source": "direct_url_workflow_dispatch",
        "website_opportunity_score": None,
        "automated_priority": "uncertain",
        "visual_review_trigger": True,
        "created_at": now_iso(),
    }

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        desktop_context = await browser.new_context(ignore_https_errors=False,
            viewport={"width": 1440, "height": 1000})
        mobile_context = await browser.new_context(ignore_https_errors=False,
            viewport={"width": 390, "height": 844}, device_scale_factor=1,
            is_mobile=True, has_touch=True)
        await mobile_context.add_init_script("""(() => {
          const ensureViewport = () => {
            if (!document.head) return;
            let viewport = document.querySelector('meta[name="viewport"]');
            if (!viewport) { viewport = document.createElement('meta'); viewport.name = 'viewport'; document.head.appendChild(viewport); }
            viewport.content = 'width=device-width, initial-scale=1';
          };
          new MutationObserver(ensureViewport).observe(document, {childList: true, subtree: true});
          ensureViewport();
        })();""")
        desktop_page = await desktop_context.new_page()
        mobile_page = await mobile_context.new_page()
        observer = PageObserver(desktop_page)
        try:
            desktop = await capture(desktop_page, requested_url, desktop_path, 1440, 1000)
            final_url = desktop_page.url
            final_host = (urlsplit(final_url).hostname or "").lower().removeprefix("www.")
            if not final_host or not public_hosts.get(final_host, await asyncio.to_thread(public_host, final_host)):
                raise ValueError("Final website host is not publicly resolvable")
            public_hosts[final_host] = True
            title = await desktop_page.title()
            image_loading = await load_lazy_images(desktop_page)
            try:
                site_checks = await run_site_checks(
                    desktop_page, final_url, observer, public_hosts, public_host, image_loading)
            except Exception as audit_error:
                site_checks = {"status": "unknown",
                    "error": f"{type(audit_error).__name__}: {audit_error}"[:500]}
            mobile = await capture(mobile_page, final_url, mobile_path, 390, 844)
            final_host = (urlsplit(desktop_page.url).hostname or "").lower().removeprefix("www.")
            if not final_host or not public_hosts.get(final_host, await asyncio.to_thread(public_host, final_host)):
                raise ValueError("Final mobile page host is not publicly resolvable")
            desktop_metrics = await viewport_metrics(desktop_page, final_url)
            mobile_metrics = await viewport_metrics(mobile_page, mobile_page.url)
            metrics = {"https": final_url.startswith("https://"), "desktop": desktop_metrics,
                       "mobile": mobile_metrics, "site_checks": site_checks,
                       "assessment_error": None if site_checks.get("status") not in {"unknown", "not_run"} else "site audit incomplete"}
            signals = score_signals(metrics)
            page_flow = {"desktop": desktop.get("page_flow", {}),
                         "mobile": mobile.get("page_flow", {})}
            hero_by_view = {key: value.get("hero_media", {}) for key, value in page_flow.items()}
            statuses = [item.get("status") for item in hero_by_view.values()]
            hero = {"status": ("uncertain" if "uncertain" in statuses else
                                "absent" if "absent" in statuses else
                                "present" if "present" in statuses else "uncertain"),
                    "trigger_views": [key for key, value in hero_by_view.items()
                                      if value.get("status") in {"absent", "uncertain"}],
                    **hero_by_view}
            record.update({
                "site_title": title,
                "site_check_status": "loaded",
                "final_url": desktop_page.url,
                "domain": final_host,
                "screenshots": {"desktop": desktop, "mobile": mobile},
                "site_checks": site_checks,
                "visual_page_flow": page_flow,
                "hero_media": hero,
                "automated_priority": signals["automated_priority"],
                "website_opportunity_score": signals["website_opportunity_score"],
                "technical_priority_before_email_bonus": signals["technical_priority_before_email_bonus"],
                "email_opportunity_bonus_points": signals["email_opportunity_bonus_points"],
                "score_basis": signals["score_basis"],
                "signals": signals["signals"],
                "opportunity_signals": signals["opportunity_signals"],
                "evidence": signals["evidence"],
                "evidence_confidence": signals["evidence_confidence"],
            })
        finally:
            observer.close()
            await desktop_context.close()
            await mobile_context.close()
            await browser.close()

    (run_dir / "CANDIDATE_LEDGER.jsonl").write_text(
        json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest = {"run_key": args.run_key, "run_type": "direct_url_assessment",
                "created_at": now_iso(), "target_count": 1,
                "capture_method": "Playwright Chromium desktop/mobile screenshots and bounded homepage script audit",
                "visual_review_dispatched": False}
    (run_dir / "RUN_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"candidate_id={candidate_id}\n")
    print(json.dumps({"run_key": args.run_key, "candidate_id": candidate_id,
                      "site_check_status": record["site_check_status"],
                      "automated_priority": record["automated_priority"],
                      "website_opportunity_score": record["website_opportunity_score"],
                      "hero_media_status": (record.get("hero_media") or {}).get("status"),
                      "site_checks_status": (record.get("site_checks") or {}).get("status")}, indent=2))
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-key", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--business-name", required=True)
    parser.add_argument("--website-url", required=True)
    parser.add_argument("--category", default="")
    parser.add_argument("--city", default="")
    parser.add_argument("--output-root", type=Path, default=Path("runs"))
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", args.run_key):
        parser.error("run key must contain only letters, digits, dot, underscore, and hyphen")
    if not re.fullmatch(r"[A-Za-z0-9]{1,30}", args.run_id):
        parser.error("run ID must contain only letters and digits")
    asyncio.run(capture_one(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
