#!/usr/bin/env python3
"""Submit one public ChatGPT web-search prompt in headed Chromium under Xvfb."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright


DEFAULT_PROMPT = (
    "Find independently owned in-ground pool companies serving Indianapolis, Indiana. "
    "Search the web and return up to 10 businesses, each with its official website URL "
    "and a source citation supporting that it serves this area. If you cannot verify "
    "the official website or service area, say so. Do not invent businesses or URLs."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--artifacts-dir", type=Path, default=Path("artifacts/chatgpt-xvfb-probe"))
    parser.add_argument("--timeout-ms", type=int, default=120_000)
    return parser.parse_args()


async def is_challenge(page, body: str) -> list[str]:
    lowered = body.lower()
    phrases = (
        "verify you are human", "verify you're human", "verify you are not a robot",
        "verify you're not a robot", "complete the captcha", "checking your browser",
        "unusual traffic", "access denied", "security check",
    )
    evidence = [f"visible challenge text: {phrase}" for phrase in phrases if phrase in lowered]
    frames = page.locator("iframe")
    for index in range(await frames.count()):
        try:
            frame = frames.nth(index)
            src = (await frame.get_attribute("src") or "").lower()
            title = (await frame.get_attribute("title") or "").lower()
            if any(part in src + " " + title for part in ("captcha", "challenge", "turnstile", "recaptcha")):
                evidence.append("challenge-related iframe present")
                break
        except Exception:
            continue
    return evidence


async def main() -> int:
    args = parse_args()
    if not args.prompt.strip():
        raise ValueError("prompt must not be empty")
    args.artifacts_dir.mkdir(parents=True, exist_ok=True)
    result_path = args.artifacts_dir / "chatgpt-result.json"
    screenshot_path = args.artifacts_dir / "chatgpt-page.png"
    result: dict = {
        "service": "ChatGPT web search",
        "prompt": args.prompt,
        "requested_url": "https://chatgpt.com/",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "browser_mode": "headed Chromium under Xvfb",
        "status": "error",
    }
    return_code = 4

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=False)
            try:
                context = await browser.new_context(
                    viewport={"width": 1440, "height": 1000}, locale="en-US"
                )
                page = await context.new_page()
                response = await page.goto(
                    "https://chatgpt.com/", wait_until="domcontentloaded", timeout=args.timeout_ms
                )
                await page.wait_for_timeout(3000)
                title = await page.title()
                final_url = page.url
                body = await page.locator("body").inner_text(timeout=15_000)
                challenges = await is_challenge(page, body)

                result.update({
                    "http_status": response.status if response else None,
                    "final_url": final_url,
                    "page_title": title,
                    "challenge_evidence": challenges,
                })
                if challenges:
                    result["status"] = "blocked"
                    print("ChatGPT presented a challenge; stopping without interaction.", flush=True)
                    return_code = 2
                else:
                    # Try common accessible/editor selectors. If the anonymous UI does not
                    # offer an input, report that state instead of attempting authentication.
                    prompt_box = page.locator(
                        "#prompt-textarea, textarea, [contenteditable='true'][data-placeholder], "
                        "[contenteditable='true'][role='textbox']"
                    ).first
                    try:
                        await prompt_box.wait_for(state="visible", timeout=15_000)
                    except Exception:
                        body_lower = body.lower()
                        result["status"] = (
                            "login_required" if "log in" in body_lower or "sign up" in body_lower
                            else "prompt_box_unavailable"
                        )
                        await page.screenshot(path=str(screenshot_path), full_page=True, timeout=20_000)
                        result.update({
                            "screenshot": screenshot_path.name,
                            "body_excerpt": body[:6000],
                            "finished_at": datetime.now(timezone.utc).isoformat(),
                        })
                        return_code = 3
                    else:
                        await prompt_box.fill(args.prompt)
                        send_button = page.get_by_role("button", name="Send", exact=False).last
                        if await send_button.count() and await send_button.is_enabled():
                            await send_button.click()
                        else:
                            await prompt_box.press("Enter")

                        # One prompt, no retry. Wait for an assistant response, then allow
                        # citations/source links to render before saving the evidence.
                        await page.locator('[data-message-author-role="assistant"]').last.wait_for(
                            state="visible", timeout=args.timeout_ms
                        )
                        await page.wait_for_timeout(12_000)
                        messages = page.locator('[data-message-author-role="assistant"]')
                        answer = (await messages.last.inner_text(timeout=15_000)).strip()
                        links = await page.locator("main a[href]").evaluate_all("""els => els.map(a => ({
                          text: (a.innerText || a.textContent || '').trim(), href: a.href
                        })).filter(x => x.href.startsWith('http') && x.text).slice(-100)""")
                        sources = []
                        seen = set()
                        for item in links:
                            if item["href"] not in seen and "chatgpt.com" not in item["href"]:
                                seen.add(item["href"])
                                sources.append(item)
                        await page.screenshot(path=str(screenshot_path), full_page=True, timeout=30_000)
                        result.update({
                            "status": "answer_received" if answer else "empty_answer",
                            "answer": answer[:20_000],
                            "source_links": sources,
                            "screenshot": screenshot_path.name,
                        })
                        return_code = 0 if answer else 3
                result["finished_at"] = datetime.now(timezone.utc).isoformat()
            finally:
                await browser.close()
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        print(result["error"], file=sys.stderr)
    finally:
        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2, ensure_ascii=False), flush=True)
    return return_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
