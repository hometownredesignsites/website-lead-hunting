#!/usr/bin/env python3
"""Recapture a candidate's desktop and mobile pages for an uncertain visual retry."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image

try:
    from .lead_hunt import host_of, public_host
    from .prepare_chatgpt_visual_review import load_candidate
except ImportError:
    from lead_hunt import host_of, public_host
    from prepare_chatgpt_visual_review import load_candidate


VIEWPORTS = {
    "desktop": {"width": 1440, "height": 1000},
    "mobile": {"width": 390, "height": 844},
}


def candidate_url(candidate: dict) -> str:
    url = str(candidate.get("final_url") or candidate.get("canonical_url") or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise ValueError("Candidate website URL is invalid") from exc
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("Candidate website URL must be an HTTP or HTTPS URL")
    if not public_host(parts.hostname):
        raise ValueError("Candidate website host is not a publicly routable host")
    return url


async def capture_viewport(browser, url: str, viewport_name: str, output_dir: Path) -> dict:
    viewport = VIEWPORTS[viewport_name]
    page = await browser.new_page(viewport=viewport, device_scale_factor=1)
    public_host_cache: dict[str, bool] = {}

    def is_public(host: str | None) -> bool:
        normalized = (host or "").lower().rstrip(".")
        if normalized not in public_host_cache:
            public_host_cache[normalized] = public_host(normalized)
        return public_host_cache[normalized]

    async def guard_public_requests(route):
        request_url = route.request.url
        try:
            parts = urlsplit(request_url)
        except ValueError:
            await route.abort()
            return
        if parts.scheme in {"http", "https"} and (
                not parts.hostname or not is_public(parts.hostname)):
            await route.abort()
            return
        await route.continue_()

    await page.route("**/*", guard_public_requests)
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        final_url = page.url
        if not is_public(host_of(final_url)):
            raise ValueError("Website redirected to a non-public host")
        if response is None:
            raise RuntimeError("Website navigation returned no main-document response")
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        try:
            await page.evaluate("() => document.fonts.ready.then(() => true)")
        except Exception:
            pass

        # Walk the page in viewport-sized increments so scroll-triggered images,
        # embeds, and background media have a chance to load before full-page capture.
        scroll_step = max(300, int(viewport["height"] * 0.75))
        visited_bottom = False
        for step_index in range(100):
            height = await page.evaluate(
                "Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
            )
            max_scroll = max(0, int(height) - viewport["height"])
            y = min(max_scroll, scroll_step * step_index)
            await page.evaluate("y => window.scrollTo(0, y)", y)
            await page.wait_for_timeout(180)
            if y >= max_scroll:
                visited_bottom = True
                break
        if not visited_bottom:
            raise RuntimeError("Page kept expanding while scrolling; fresh capture was stopped at the safety limit")

        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(500)
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / f"{viewport_name}.png"
        await page.screenshot(path=str(target), full_page=True, animations="disabled", timeout=45000)
        with Image.open(target) as image:
            width, height = image.size
            image.verify()
        return {
            "status": "captured",
            "viewport": viewport_name,
            "width": width,
            "height": height,
            "bytes": target.stat().st_size,
            "http_status": response.status,
            "final_url": final_url,
            "path": target.name,
        }
    finally:
        await page.close()


async def recapture(url: str, output_dir: Path) -> dict:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        try:
            results = []
            for viewport_name in ("desktop", "mobile"):
                results.append(await capture_viewport(browser, url, viewport_name, output_dir))
            return {"status": "captured", "screenshots": results}
        finally:
            await browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    candidate = load_candidate(args.run_dir, args.candidate_id)
    url = candidate_url(candidate)
    result = asyncio.run(recapture(url, args.output_dir.resolve()))
    result.update({"candidate_id": args.candidate_id, "requested_url": url})
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "recapture-manifest.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
