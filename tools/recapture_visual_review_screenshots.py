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
MIN_VERIFIED_SCROLL_PX = 150
PAGE_DOWN_ATTEMPTS = 2


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

        # Save the true top viewport, then scroll as a user would. Keyboard Page
        # Down and wheel input also work with sites that use a nested scroller,
        # where window.scrollTo() can report zero movement.
        output_dir.mkdir(parents=True, exist_ok=True)
        top_target = output_dir / f"{viewport_name}_top.png"
        mid_target = output_dir / f"{viewport_name}_mid.png"
        await page.screenshot(path=str(top_target), full_page=False, animations="disabled", timeout=45000)
        before_state = await page.evaluate("""() => {
            const root = document.scrollingElement || document.documentElement;
            return { windowY: window.scrollY, rootY: root.scrollTop,
                rootMax: Math.max(0, root.scrollHeight - root.clientHeight) };
        }""")
        await page.evaluate("""() => {
            const body = document.body;
            if (body) { body.setAttribute('tabindex', '-1'); body.focus({preventScroll: true}); }
        }""")
        observed_positions = []
        for method in ("PageDown", "wheel"):
            for _ in range(PAGE_DOWN_ATTEMPTS):
                if method == "PageDown":
                    await page.keyboard.press("PageDown")
                else:
                    await page.mouse.move(viewport["width"] // 2, viewport["height"] // 2)
                    await page.mouse.wheel(0, int(viewport["height"] * 0.8))
                await page.wait_for_timeout(300)
                state = await page.evaluate("""() => {
                    const root = document.scrollingElement || document.documentElement;
                    const nested = Array.from(document.querySelectorAll('*'))
                        .filter(el => el.scrollHeight > el.clientHeight + 40 &&
                            ['auto', 'scroll'].includes(getComputedStyle(el).overflowY))
                        .map(el => ({top: el.scrollTop, max: el.scrollHeight - el.clientHeight}))
                        .filter(item => item.top > 0);
                    return {windowY: window.scrollY, rootY: root.scrollTop,
                        nested, rootMax: Math.max(0, root.scrollHeight - root.clientHeight)};
                }""")
                observed_positions.append({"method": method, **state})
                if (state["windowY"] >= MIN_VERIFIED_SCROLL_PX or
                        state["rootY"] >= MIN_VERIFIED_SCROLL_PX or
                        any(item["top"] >= MIN_VERIFIED_SCROLL_PX for item in state["nested"])):
                    break
            if observed_positions and (
                    observed_positions[-1]["windowY"] >= MIN_VERIFIED_SCROLL_PX or
                    observed_positions[-1]["rootY"] >= MIN_VERIFIED_SCROLL_PX or
                    any(item["top"] >= MIN_VERIFIED_SCROLL_PX for item in observed_positions[-1]["nested"])):
                break
        await page.screenshot(path=str(mid_target), full_page=False, animations="disabled", timeout=45000)
        with Image.open(top_target) as image:
            width, height = image.size
            image.verify()
        with Image.open(mid_target) as image:
            if image.size != (viewport["width"], viewport["height"]):
                raise RuntimeError(f"Unexpected {viewport_name} mid screenshot dimensions: {image.size}")
            image.verify()
        final_state = observed_positions[-1] if observed_positions else before_state
        window_movement = max((int(item.get("windowY", 0)) for item in observed_positions), default=0)
        root_movement = max((int(item.get("rootY", 0)) for item in observed_positions), default=0)
        nested_movement = max((int(item.get("top", 0)) for state in observed_positions
                               for item in state.get("nested", [])), default=0)
        max_observed = max(window_movement, root_movement, nested_movement)
        return {
            "status": "captured",
            "viewport": viewport_name,
            "width": width,
            "height": height,
            "bytes": top_target.stat().st_size + mid_target.stat().st_size,
            "http_status": response.status,
            "final_url": final_url,
            "path": top_target.name,
            "mid_path": mid_target.name,
            "scroll_validation": {
                "method": "PageDown+wheel",
                "max_window_scroll_px": int(final_state.get("rootMax", 0)),
                "max_observed_scroll_px": max_observed,
                "minimum_required_scroll_px": MIN_VERIFIED_SCROLL_PX,
                "verified": max_observed >= MIN_VERIFIED_SCROLL_PX,
                "positions": observed_positions,
            },
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
