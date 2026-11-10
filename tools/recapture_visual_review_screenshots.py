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
    from .page_flow import inspect_page_flow
except ImportError:
    from lead_hunt import host_of, public_host
    from prepare_chatgpt_visual_review import load_candidate
    from page_flow import inspect_page_flow


VIEWPORTS = {
    # Match the exact crop dimensions consumed by prepare_chatgpt_visual_review.py.
    "desktop": {"width": 1440, "height": 900},
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
        flow = await inspect_page_flow(page)
        top_page_flow = flow
        target_terms = ("project", "portfolio", "gallery", "our work", "recent work", "before", "after",
                        "remodel", "renovation", "service")
        def find_target(images):
            usable = [item for item in images if isinstance(item, dict)
                      and item.get("document_y") is not None
                      and item.get("width", 0) >= 160 and item.get("height", 0) >= 100]
            labeled = [item for item in usable
                       if any(term in " ".join(str(item.get(key) or "") for key in ("alt", "src")).casefold()
                              for term in target_terms)]
            below_fold_labeled = next((item for item in labeled
                                       if item["document_y"] >= viewport["height"] * 0.8), None)
            if below_fold_labeled:
                return below_fold_labeled
            below_fold = next((item for item in usable
                               if item["document_y"] >= viewport["height"] * 0.8), None)
            return below_fold or (labeled[0] if labeled else None)

        target_image = find_target(flow.get("content_images") or [])
        discovery_steps = 0
        # Scroll progressively to trigger lazy loading, then inspect the newly
        # revealed content before deciding where the mid screenshot belongs.
        while target_image is None and discovery_steps < 8:
            await page.keyboard.press("PageDown")
            await page.wait_for_timeout(350)
            discovery_steps += 1
            flow = await inspect_page_flow(page)
            target_image = find_target(flow.get("content_images") or [])
        usable_images = [item for item in (flow.get("content_images") or [])
                         if isinstance(item, dict) and item.get("document_y") is not None
                         and item.get("width", 0) >= 160 and item.get("height", 0) >= 100]
        if target_image is None and usable_images:
            target_image = usable_images[0]
        if target_image:
            target_scroll = max(MIN_VERIFIED_SCROLL_PX,
                                int(target_image["document_y"] - viewport["height"] * 0.28))
            await page.evaluate("y => window.scrollTo({top: y, behavior: 'instant'})", target_scroll)
            await page.wait_for_timeout(500)
            capture_target = {
                "type": target_image.get("type", "image"),
                "document_y": target_image["document_y"],
                "alt": target_image.get("alt", ""),
                "scroll_target_px": target_scroll,
                "lazy_load_discovery_steps": discovery_steps,
                "strategy": "first_meaningful_content_image",
            }
        else:
            capture_target = {"strategy": "page_down_fallback", "reason": "no meaningful content image found",
                             "lazy_load_discovery_steps": discovery_steps}
            await page.keyboard.press("PageDown")
            await page.wait_for_timeout(400)
        async def measure_scroll_state() -> dict:
            return await page.evaluate("""() => {
                const root = document.scrollingElement || document.documentElement;
                const nested = Array.from(document.querySelectorAll('*'))
                    .filter(el => el.scrollHeight > el.clientHeight + 40 &&
                        ['auto', 'scroll'].includes(getComputedStyle(el).overflowY))
                    .map(el => ({top: el.scrollTop, max: el.scrollHeight - el.clientHeight,
                        tag: el.tagName.toLowerCase(), id: el.id || '',
                        className: typeof el.className === 'string' ? el.className.slice(0, 120) : ''}))
                    .filter(item => item.max > 0);
                return {windowY: window.scrollY, rootY: root.scrollTop,
                    nested, rootMax: Math.max(0, root.scrollHeight - root.clientHeight)};
            }""")

        def scroll_depth(state: dict) -> int:
            return max(
                int(state.get("windowY") or 0),
                int(state.get("rootY") or 0),
                max((int(item.get("top") or 0) for item in state.get("nested", [])), default=0),
            )

        def scroll_range(state: dict) -> int:
            return max(
                int(state.get("rootMax") or 0),
                max((int(item.get("max") or 0) for item in state.get("nested", [])), default=0),
            )

        observed_positions = []
        if target_image:
            observed_positions.append({"method": "content-image-target", **await measure_scroll_state()})
        else:
            await page.evaluate("""() => {
                const body = document.body;
                if (body) { body.setAttribute('tabindex', '-1'); body.focus({preventScroll: true}); }
            }""")

        # A below-the-fold image can exist inside a nested scroll region, where
        # window.scrollTo() reports no movement. Verify the current position and
        # try bounded user-like input even when a content-image target was found.
        if not observed_positions or scroll_depth(observed_positions[-1]) < MIN_VERIFIED_SCROLL_PX:
            for method in ("PageDown", "wheel", "wheel"):
                if method == "PageDown":
                    await page.keyboard.press("PageDown")
                else:
                    await page.mouse.move(viewport["width"] // 2, viewport["height"] // 2)
                    await page.mouse.wheel(0, int(viewport["height"] * 0.8))
                await page.wait_for_timeout(350)
                state = await measure_scroll_state()
                observed_positions.append({"method": method, **state})
                if scroll_depth(state) >= MIN_VERIFIED_SCROLL_PX:
                    break

        capture_state = await measure_scroll_state()
        observed_positions.append({"method": "screenshot-capture", **capture_state})
        methods_used = [item["method"] for item in observed_positions
                        if item.get("method") != "screenshot-capture"]
        await page.screenshot(path=str(mid_target), full_page=False, animations="disabled", timeout=45000)
        with Image.open(top_target) as image:
            width, height = image.size
            image.verify()
        with Image.open(mid_target) as image:
            if image.size != (viewport["width"], viewport["height"]):
                raise RuntimeError(f"Unexpected {viewport_name} mid screenshot dimensions: {image.size}")
            image.verify()
        max_observed = max((scroll_depth(state) for state in observed_positions), default=0)
        capture_scroll_px = scroll_depth(capture_state)
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
            "page_flow": top_page_flow,
            "hero_media": top_page_flow.get("hero_media", {}),
            "mid_capture_target": capture_target,
            "scroll_validation": {
                "method": "+".join(methods_used),
                "max_window_scroll_px": int(capture_state.get("windowY", 0)),
                "max_observed_scroll_px": max_observed,
                "capture_scroll_px": capture_scroll_px,
                "scrollable_range_px": scroll_range(capture_state),
                "minimum_required_scroll_px": MIN_VERIFIED_SCROLL_PX,
                "verified": capture_scroll_px >= MIN_VERIFIED_SCROLL_PX,
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
