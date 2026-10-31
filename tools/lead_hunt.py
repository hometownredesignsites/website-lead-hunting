#!/usr/bin/env python3
"""Resumable local-business website discovery and evidence capture for GitHub Actions."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import re
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit



DDG_SEARCH_URL = "https://html.duckduckgo.com/html/"
DIRECTORY_HOSTS = {
    "angi.com", "bbb.org", "facebook.com", "foursquare.com", "homeadvisor.com",
    "homeguide.com", "houzz.com", "instagram.com", "mapquest.com", "manta.com",
    "nextdoor.com", "pinterest.com", "reddit.com", "thumbtack.com", "tripadvisor.com",
    "yellowpages.com", "yelp.com", "trustanalytica.org", "contractorsranked.com",
    "kitchenremodelingranked.com", "localremodelingquotes.com", "bestofdenver.org",
}
EDITORIAL_HOSTS = {"westword.com", "visitdenver.com"}
SOCIAL_HOSTS = {"youtube.com", "tiktok.com", "x.com", "twitter.com"}
TRACKING_KEYS = {"gclid", "fbclid", "msclkid", "srsltid", "ref", "referrer", "source"}
SCORE_WEIGHTS = {
    "no_https": 10,
    "missing_viewport": 15,
    "mobile_overflow": 20,
    "old_copyright": 10,
    "no_contact_path": 5,
    "no_clear_cta": 10,
    "weak_metadata": 3,
    "failed_images": 10,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")[:90] or "item"


def split_values(value: str) -> list[str]:
    values = [line.strip() for line in re.split(r"[\r\n]+", value) if line.strip()]
    if not values:
        raise ValueError("At least one non-empty value is required")
    if len({v.casefold() for v in values}) != len(values):
        raise ValueError("Input contains duplicate values")
    return values


def host_of(url: str | None) -> str:
    try:
        return (urlsplit(url or "").hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def is_excluded_host(host: str, exclusions: set[str] | None = None) -> bool:
    host = (host or "").lower().removeprefix("www.").rstrip(".")
    return any(host == domain or host.endswith("." + domain)
               for domain in (DIRECTORY_HOSTS | (exclusions or set())))


def resolve_ddg_result(raw_url: str, card_is_ad: bool = False) -> dict:
    """Unwrap DDG links and recognize Bing-backed sponsored-result wrappers."""
    current = raw_url or ""
    is_ad = card_is_ad
    ad_domain = ""
    for _ in range(5):
        try:
            parts = urlsplit(current)
        except ValueError:
            break
        host = (parts.hostname or "").lower()
        query = parse_qs(parts.query)
        if host.endswith("duckduckgo.com"):
            if "ad_provider" in query or "ad_type" in query or parts.path.endswith("/y.js"):
                is_ad = True
                ad_domain = (query.get("ad_domain") or [ad_domain])[0]
            wrapped = (query.get("uddg") or query.get("url") or [None])[0]
            if wrapped:
                current = wrapped
                continue
            if parts.path.endswith("/y.js"):
                break
        break

    try:
        parts = urlsplit(current)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower().removeprefix("www.").rstrip(".")
    except ValueError:
        return {"raw_url": raw_url, "url": None, "domain": "", "is_ad": is_ad,
                "ad_domain": ad_domain, "resolution": "invalid_url"}
    if is_ad:
        landing_host = ad_domain.lower().removeprefix("www.") if ad_domain else host
        return {"raw_url": raw_url, "url": None, "domain": landing_host,
                "is_ad": True, "ad_domain": landing_host, "resolution": "sponsored"}
    if scheme not in {"http", "https"} or not host or host.endswith("duckduckgo.com"):
        return {"raw_url": raw_url, "url": None, "domain": host,
                "is_ad": False, "ad_domain": "", "resolution": "unresolved"}
    kept = [(key, value) for key, values in parse_qs(parts.query, keep_blank_values=True).items()
            if key.lower() not in TRACKING_KEYS and not key.lower().startswith("utm_")
            for value in values]
    clean_query = urlencode(kept, doseq=True)
    clean_url = urlunsplit((scheme, parts.netloc, parts.path or "/", clean_query, ""))
    return {"raw_url": raw_url, "url": clean_url, "domain": host,
            "is_ad": False, "ad_domain": "", "resolution": "resolved"}


def classify_result(result: dict, exclusions: set[str] | None = None) -> tuple[str, str]:
    if result.get("is_ad"):
        return "sponsored_ad", "DuckDuckGo/Bing marked this result as a paid ad"
    url, host = result.get("url"), result.get("domain", "")
    if not url or not host:
        return "unresolved_result", "The result URL could not be resolved to a public website"
    if is_excluded_host(host, exclusions):
        return "directory_or_lead_generation", f"Excluded directory or lead-generation host: {host}"
    if host in SOCIAL_HOSTS or any(host.endswith("." + d) for d in SOCIAL_HOSTS):
        return "social_or_video", f"Social/video host: {host}"
    title = (result.get("title") or "").lower()
    path = urlsplit(url).path.lower()
    editorial_path = any(part in path for part in ("/blog/", "/news/", "/guide/", "/best-of/", "/articles/"))
    listicle_title = any(word in title for word in ("best ", "top ", "ranked", "guide to", "directory"))
    if host in EDITORIAL_HOSTS or (editorial_path and listicle_title):
        return "editorial_or_listicle", f"Editorial/list result rather than a direct business website: {host}"
    return "eligible_for_site_check", "Direct-site check required; business and market fit remain unverified"


def score_signals(metrics: dict) -> dict:
    """Score observable technical signals; never treat this as a visual verdict."""
    desktop = metrics.get("desktop", {})
    mobile = metrics.get("mobile", {})
    checks = {
        "no_https": (not metrics.get("https")) if "https" in metrics else None,
        "missing_viewport": (not desktop.get("viewportMeta")) if "viewportMeta" in desktop else None,
        "mobile_overflow": (mobile.get("horizontalOverflowPx", 0) > 12) if "horizontalOverflowPx" in mobile else None,
        "old_copyright": (any(year <= datetime.now().year - 5 for year in desktop.get("copyrightYears", []))) if desktop.get("copyrightYears") else None,
        "no_contact_path": (not desktop.get("hasContactPath")) if "hasContactPath" in desktop else None,
        "no_clear_cta": (not desktop.get("hasCta")) if "hasCta" in desktop else None,
        "weak_metadata": (not (desktop.get("titlePresent") and desktop.get("descriptionPresent"))) if "titlePresent" in desktop else None,
        "failed_images": (desktop.get("imageCount", 0) >= 4 and
                          desktop.get("loadedImageCount", 0) / max(1, desktop.get("imageCount", 0)) < 0.75) if "imageCount" in desktop else None,
    }
    applicable = {key: value for key, value in checks.items() if value is not None}
    total = sum(SCORE_WEIGHTS[key] for key in applicable)
    earned = sum(SCORE_WEIGHTS[key] for key, issue in applicable.items() if issue)
    score = round(100 * earned / total) if total else None
    enough = len(applicable) >= 5 and not metrics.get("assessment_error")
    priority = "uncertain" if not enough else "high" if score >= 61 else "medium" if score >= 41 else "pass"
    return {"automated_priority": priority, "website_opportunity_score": score,
            "score_basis": "normalized score from directly observable technical signals; visual judgment is pending",
            "signals": {key: "unknown" if value is None else "issue" if value else "clear" for key, value in checks.items()},
            "evidence": metrics, "evidence_confidence": "sufficient" if enough else "limited"}


def parse_search_results(items: list[dict]) -> list[dict]:
    results, seen = [], set()
    for item in items:
        raw = (item.get("href") or "").strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)
        resolved = resolve_ddg_result(raw, bool(item.get("is_ad")))
        results.append({"position": len(results) + 1, "title": (item.get("title") or "")[:300],
                        "snippet": (item.get("snippet") or "")[:1200], **resolved,
                        "result_disposition": None, "disposition_reason": None})
    return results


def fingerprint(queries: list[dict], region: str, engine: str = "duckduckgo_html") -> str:
    payload = json.dumps({"queries": queries, "region": region, "engine": engine}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def atomic_jsonl(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    tmp.replace(path)


def build_queries(categories_text: str, locations_text: str, region: str) -> list[dict]:
    categories, locations = split_values(categories_text), split_values(locations_text)
    return [{"query_index": i, "category_index": ci, "category": category,
             "location_index": li, "location": location,
             "query": f"{category} {location}"}
            for i, (ci, category, li, location) in enumerate(
                ((ci, c, li, loc) for ci, c in enumerate(categories, 1)
                 for li, loc in enumerate(locations, 1)), 1)]


def public_host(host: str) -> bool:
    host = (host or "").strip("[]").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
        return bool(addresses) and all(ipaddress.ip_address(value).is_global for value in addresses)
    except (OSError, ValueError):
        return False


def challenge_text_reason(url: str, title: str, body: str) -> str | None:
    lowered = (body or "").lower()
    if "anomaly.js" in (url or "").lower():
        return "DuckDuckGo anomaly challenge URL"
    if any(term in lowered for term in ("complete the following challenge", "select all squares",
                                        "bots use duckduckgo too", "search was made by a human")):
        return "DuckDuckGo human-verification challenge text"
    if any(term in lowered for term in ("verify you are not a robot", "verify you are human",
                                       "complete the captcha", "unusual traffic")):
        return "Visible search-provider verification/challenge text"
    if "captcha" in lowered and any(term in lowered for term in ("automated", "robot", "human")):
        return "Visible CAPTCHA text"
    if "challenge" in lowered and any(term in lowered for term in ("robot", "human", "duckduckgo")):
        return "Visible search-provider challenge text"
    if "sorry" in (title or "").lower() and "verify" in lowered:
        return "Challenge title and verification text"
    return None


async def visible_challenge(page, url: str, title: str, body: str) -> str | None:
    reason = challenge_text_reason(url, title, body)
    if reason:
        return reason
    frames = page.locator("iframe[src*='recaptcha'], iframe[title*='captcha']")
    try:
        for index in range(min(await frames.count(), 5)):
            if await frames.nth(index).is_visible():
                return "Visible verification iframe"
    except Exception:
        pass
    return None


async def extract_ddg_results(page) -> list[dict]:
    return await page.evaluate("""() => {
      const root = document.querySelector('#links') || document.querySelector('#links-wrapper') || document.body;
      const visible = el => !!(el && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
      return [...root.querySelectorAll('.result')].map(box => {
        const a = box.querySelector('.result__a') || box.querySelector('a[href]');
        if (!a || !visible(a)) return null;
        const snippet = box.querySelector('.result__snippet');
        const text = (a.innerText || a.textContent || '').trim();
        const body = (snippet && (snippet.innerText || snippet.textContent) || '').trim();
        return {href: a.href, title: text, snippet: body,
          is_ad: box.matches('.result--ad') || !!box.querySelector('[data-testid*="ad"], .result__ad')};
      }).filter(x => x && x.href && /^https?:/.test(x.href) && x.title.length > 2);
    }""")


async def screenshot_with_fallback(page, path: Path, full_timeout_ms: int = 30000) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(path), full_page=True, animations="disabled", timeout=full_timeout_ms)
        saved_path = path.resolve()
        try:
            display_path = saved_path.relative_to(Path.cwd().resolve()).as_posix()
        except ValueError:
            display_path = saved_path.as_posix()
        return {"path": display_path, "mode": "full_page", "status": "saved"}
    except Exception as full_error:
        fallback_path = path.with_name(path.stem + "_viewport" + path.suffix)
        try:
            await page.screenshot(path=str(fallback_path), full_page=False, animations="disabled", timeout=15000)
            saved_path = fallback_path.resolve()
            try:
                display_path = saved_path.relative_to(Path.cwd().resolve()).as_posix()
            except ValueError:
                display_path = saved_path.as_posix()
            return {"path": display_path, "mode": "viewport_fallback",
                    "status": "saved", "full_page_error": str(full_error)[:400]}
        except Exception as view_error:
            return {"path": None, "mode": "none", "status": "error",
                    "full_page_error": str(full_error)[:400], "viewport_error": str(view_error)[:400]}


async def page_metrics(page) -> dict:
    return await page.evaluate("""() => {
      const text = document.body?.innerText || '';
      return {
        viewportMeta: !!document.querySelector('meta[name="viewport"]'),
        documentWidth: document.documentElement.scrollWidth,
        viewportWidth: document.documentElement.clientWidth,
        imageCount: [...document.images].length,
        loadedImageCount: [...document.images].filter(i => i.complete && i.naturalWidth > 0).length,
        hasContactPath: [...document.querySelectorAll('a[href]')].some(a => /contact|location|visit|call|phone/i.test((a.innerText||'')+' '+a.href)),
        hasCta: [...document.querySelectorAll('a,button')].some(a => /contact|call|visit|book|schedule|request|get a quote|estimate|get in touch/i.test((a.innerText||'').trim())),
        titlePresent: !!document.title.trim(),
        descriptionPresent: !!document.querySelector('meta[name="description"]')?.content.trim(),
        copyrightYears: [...text.matchAll(/(?:©|copyright)\\s*(?:\\d{4}\\s*[-–]\\s*)?(20\\d{2})/ig)].map(m => Number(m[1]))
      };
    }""")


async def assess_site(page, result: dict, query: dict, run_dir: Path,
                      public_host_cache: dict[str, bool]) -> dict:
    url = result["url"]
    requested_host = result["domain"]
    if requested_host not in public_host_cache:
        public_host_cache[requested_host] = await asyncio.to_thread(public_host, requested_host)
    if not public_host_cache[requested_host]:
        return {"site_check_status": "blocked_non_public_host", "domain": requested_host,
                "reason": "Host is local, private, or did not resolve exclusively to public IP addresses"}
    metrics: dict = {"https": urlsplit(url).scheme == "https"}
    shots: dict = {}
    try:
        await page.set_viewport_size({"width": 1440, "height": 1000})
        response = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(900)
        final_url = page.url
        final_host = host_of(final_url)
        if is_excluded_host(final_host):
            return {"site_check_status": "redirected_to_excluded_host", "domain": final_host,
                    "final_url": final_url, "reason": f"Site redirected to excluded host {final_host}"}
        if final_host not in public_host_cache:
            public_host_cache[final_host] = await asyncio.to_thread(public_host, final_host)
        if not public_host_cache[final_host]:
            return {"site_check_status": "redirected_to_non_public_host", "domain": final_host,
                    "final_url": final_url, "reason": "Final host is local, private, or unresolved"}
        metrics["http_status"] = response.status if response else None
        metrics["final_url"] = final_url
        metrics["title"] = await page.title()
        metrics["desktop"] = await page_metrics(page)
        stem = slugify(final_host)
        desktop = await screenshot_with_fallback(page, run_dir / "screenshots" / f"{stem}_desktop.png")
        shots["desktop"] = desktop

        await page.set_viewport_size({"width": 390, "height": 844})
        await page.wait_for_timeout(350)
        metrics["mobile"] = await page_metrics(page)
        mobile = await screenshot_with_fallback(page, run_dir / "screenshots" / f"{stem}_mobile.png", 25000)
        shots["mobile"] = mobile
        signals = score_signals(metrics)
        return {"site_check_status": "loaded", "domain": final_host, "final_url": final_url,
                "site_title": metrics.get("title", ""), "automated_priority": signals["automated_priority"],
                "website_opportunity_score": signals["website_opportunity_score"],
                "score_basis": signals["score_basis"], "signals": signals["signals"],
                "evidence": metrics, "screenshots": shots, "evidence_confidence": signals["evidence_confidence"],
                "hermes_visual_review": "pending", "human_review": "pending"}
    except Exception as exc:
        metrics["assessment_error"] = f"{type(exc).__name__}: {exc}"[:700]
        signals = score_signals(metrics)
        return {"site_check_status": "error", "domain": host_of(page.url) or requested_host,
                "final_url": page.url, "automated_priority": signals["automated_priority"],
                "website_opportunity_score": signals["website_opportunity_score"],
                "score_basis": signals["score_basis"], "signals": signals["signals"],
                "evidence": metrics, "screenshots": shots,
                "evidence_confidence": "limited", "hermes_visual_review": "pending",
                "human_review": "pending"}


def query_is_complete(row: dict) -> bool:
    return (row.get("terminal_status") == "success" and
            all(item.get("result_disposition") for item in row.get("organic_results", [])))


def save_run(run_dir: Path, queries: list[dict], records: dict[int, dict], candidates: list[dict],
             manifest: dict, blocked: bool = False) -> dict:
    atomic_jsonl(run_dir / "QUERY_LEDGER.jsonl", [records[i] for i in sorted(records)])
    atomic_jsonl(run_dir / "CANDIDATE_LEDGER.jsonl", candidates)
    statuses = [records[i].get("terminal_status") for i in records]
    complete = len(records) >= len(queries) and all(query_is_complete(records[i]) for i in range(1, len(queries) + 1))
    progress = {"run_key": run_dir.name, "region": manifest["region"], "engine": manifest["engine"],
                "total_queries": len(queries), "recorded_queries": len(records),
                "successful": statuses.count("success"), "blocked": statuses.count("blocked"),
                "errors": statuses.count("error") + statuses.count("no_results"),
                "pending": max(0, len(queries) - statuses.count("success") - statuses.count("blocked")),
                "status": "COMPLETE" if complete else "BLOCKED" if blocked else "IN_PROGRESS",
                "updated_at": now_iso()}
    atomic_json(run_dir / "LOCATION_CATEGORY_PROGRESS.json", progress)
    atomic_json(run_dir / "RUN_MANIFEST.json", manifest)
    return progress


async def run_batch(args: argparse.Namespace) -> int:
    from playwright.async_api import async_playwright

    run_dir = args.state_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    categories, locations = split_values(args.categories), split_values(args.locations)
    queries = [{"query_index": i, "category_index": ci, "category": category,
                "location_index": li, "location": location, "query": f"{category} {location}"}
               for i, (ci, category, li, location) in enumerate(
                   ((ci, c, li, loc) for ci, c in enumerate(categories, 1)
                    for li, loc in enumerate(locations, 1)), 1)]
    if len(queries) > 200:
        raise ValueError("Maximum configured batch is 200 category/location queries")
    if args.max_queries < 1 or args.max_queries > 20:
        raise ValueError("max-queries must be between 1 and 20")
    if args.max_sites_per_query < 1 or args.max_sites_per_query > 8:
        raise ValueError("max-sites-per-query must be between 1 and 8")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", run_dir.name):
        raise ValueError("state-dir folder name must be a safe run key (letters, digits, dot, underscore, hyphen)")
    input_hash = fingerprint(queries, args.region)
    manifest_path = run_dir / "RUN_MANIFEST.json"
    manifest = {"run_key": run_dir.name, "region": args.region, "engine": "duckduckgo_html",
                "search_url": DDG_SEARCH_URL, "categories": categories, "locations": locations,
                "configured_queries": len(queries), "input_fingerprint": input_hash,
                "workflow": "GitHub Actions headed Chromium under Xvfb",
                "created_at": now_iso(), "updated_at": now_iso(),
                "automated_scoring_is_final": False,
                "review_policy": "Hermes visual inference and human final review are separate local steps"}
    if manifest_path.exists():
        prior = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        if prior.get("input_fingerprint") != input_hash:
            raise ValueError("This run key already contains different categories/locations/region; choose a new run key")
        manifest = {**prior, "updated_at": now_iso()}
    records = {int(row["query_index"]): row for row in read_jsonl(run_dir / "QUERY_LEDGER.jsonl")
               if row.get("query_index") is not None}
    candidates = read_jsonl(run_dir / "CANDIDATE_LEDGER.jsonl")
    candidate_by_domain = {row.get("domain", "").lower(): row for row in candidates if row.get("domain")}
    exclusions = set()
    exclusion_path = Path(__file__).resolve().parents[1] / "config" / "excluded_domains.txt"
    if exclusion_path.exists():
        exclusions = {line.strip().lower().removeprefix("www.") for line in exclusion_path.read_text(encoding="utf-8").splitlines()
                      if line.strip() and not line.strip().startswith("#")}
    public_host_cache: dict[str, bool] = {}
    pending = [q for q in queries if not query_is_complete(records.get(q["query_index"], {}))]
    batch = pending[:args.max_queries]
    if not batch:
        print("All configured queries are already complete; no search was repeated.")
        progress = save_run(run_dir, queries, records, candidates, manifest)
        print(json.dumps(progress, indent=2))
        return 0

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US")

        async def guard_local_navigation(route):
            request = route.request
            if not request.is_navigation_request():
                await route.continue_()
                return
            host = host_of(request.url)
            if host not in public_host_cache:
                public_host_cache[host] = await asyncio.to_thread(public_host, host)
            if public_host_cache[host]:
                await route.continue_()
            else:
                await route.abort("blockedbyclient")

        await context.route("**/*", guard_local_navigation)
        page = await context.new_page()
        blocked = False
        for query in batch:
            idx = query["query_index"]
            attempt_history = records.get(idx, {}).get("attempt_history", [])
            search_url = DDG_SEARCH_URL + "?" + urlencode({"q": query["query"]})
            row = {"query_index": idx, **query, "search_engine": "duckduckgo_html",
                   "requested_url": search_url, "actual_url": None, "page_title": None,
                   "challenge_status": "unknown", "terminal_status": "in_progress",
                   "organic_results_returned_count": 0, "organic_results_inspected_count": 0,
                   "organic_results": [], "attempt_history": attempt_history,
                   "started_at": now_iso(), "timestamp": now_iso()}
            records[idx] = row
            save_run(run_dir, queries, records, candidates, manifest)
            print(f"Searching {idx}/{len(queries)}: {query['query']}", flush=True)
            try:
                response = await page.goto(search_url, wait_until="domcontentloaded", timeout=35000)
                await page.wait_for_timeout(1500)
                body = await page.locator("body").inner_text(timeout=10000)
                title = await page.title()
                reason = await visible_challenge(page, page.url, title, body)
                row.update({"actual_url": page.url, "page_title": title,
                            "http_status": response.status if response else None,
                            "challenge_status": "blocked" if reason else "none_visible",
                            "challenge_evidence": reason})
                serp_path = run_dir / "screenshots" / f"query_{idx:04d}_serp.png"
                serp_shot = await screenshot_with_fallback(page, serp_path, 15000)
                row["serp_screenshot"] = serp_shot
                if reason:
                    row.update({"terminal_status": "blocked", "finished_at": now_iso()})
                    blocked = True
                    save_run(run_dir, queries, records, candidates, manifest, blocked=True)
                    print(f"Search challenge detected; stopped at query {idx}: {reason}", flush=True)
                    break
                results = parse_search_results(await extract_ddg_results(page))
                row["organic_results_returned_count"] = len(results)
                if not results:
                    row.update({"terminal_status": "no_results", "finished_at": now_iso(),
                                "error": "No organic result cards were extracted; inspect SERP screenshot and selectors"})
                    records[idx] = row
                    save_run(run_dir, queries, records, candidates, manifest)
                    continue
                row["reported_results_text"] = next((m.group(0) for m in re.finditer(r"[\\d,]+\\s+results", body, re.I)), None)
                inspected = 0
                eligible_sites = 0
                for result in results:
                    disposition, reason_text = classify_result(result, exclusions)
                    result["result_disposition"] = disposition
                    result["disposition_reason"] = reason_text
                    if disposition != "eligible_for_site_check" or eligible_sites >= args.max_sites_per_query:
                        if disposition == "eligible_for_site_check":
                            result["result_disposition"] = "eligible_not_visited_limit"
                            result["disposition_reason"] = f"Per-query site-check limit ({args.max_sites_per_query}) reached"
                        inspected += 1
                        row["organic_results"] = results
                        row["organic_results_inspected_count"] = inspected
                        row["updated_at"] = now_iso()
                        records[idx] = row
                        save_run(run_dir, queries, records, candidates, manifest)
                        continue
                    host = result["domain"]
                    if host in candidate_by_domain:
                        result["result_disposition"] = "duplicate_domain"
                        result["disposition_reason"] = "This domain was already assessed earlier in the run"
                        result["candidate_domain"] = host
                    else:
                        eligible_sites += 1
                        site = await assess_site(page, result, query, run_dir, public_host_cache)
                        if site.get("domain") and site["domain"] in candidate_by_domain:
                            result["result_disposition"] = "duplicate_domain"
                            result["disposition_reason"] = "Redirected to a domain already assessed earlier in the run"
                            result["candidate_domain"] = site["domain"]
                        elif site.get("site_check_status") in {"redirected_to_excluded_host", "redirected_to_non_public_host", "blocked_non_public_host"}:
                            result["result_disposition"] = site["site_check_status"]
                            result["disposition_reason"] = site.get("reason", "Destination excluded")
                        else:
                            candidate = {"candidate_id": f"{idx:04d}-{slugify(site.get('domain') or host)}",
                                         "source_query_index": idx, "category": query["category"],
                                         "configured_location": query["location"], "region": args.region,
                                         "business_name": result["title"], "domain": site.get("domain", host),
                                         "canonical_url": f"{urlsplit(site.get('final_url') or result['url']).scheme or 'https'}://{site.get('domain', host)}",
                                         "source_result_url": result["raw_url"], "source_result_title": result["title"],
                                         "source_result_snippet": result["snippet"], "source_result_position": result["position"],
                                         **site, "market_evidence": "pending_review", "human_review": "pending",
                                         "hermes_visual_review": "pending", "recorded_at": now_iso()}
                            candidates.append(candidate)
                            candidate_by_domain[candidate["domain"].lower()] = candidate
                            result["candidate_domain"] = candidate["domain"]
                            result["result_disposition"] = "site_assessed"
                            result["disposition_reason"] = "Direct site checked; market fit and final redesign verdict require review"
                    inspected += 1
                    row["organic_results"] = results
                    row["organic_results_inspected_count"] = inspected
                    row["updated_at"] = now_iso()
                    records[idx] = row
                    save_run(run_dir, queries, records, candidates, manifest)
                row.update({"terminal_status": "success", "finished_at": now_iso(),
                            "organic_results": results,
                            "organic_results_inspected_count": inspected})
                records[idx] = row
                save_run(run_dir, queries, records, candidates, manifest)
                print(f"Saved query {idx}: {len(results)} results, {eligible_sites} direct sites checked", flush=True)
                await page.wait_for_timeout(1200)
            except Exception as exc:
                row.update({"terminal_status": "error", "finished_at": now_iso(),
                            "error": f"{type(exc).__name__}: {exc}"[:1200]})
                records[idx] = row
                save_run(run_dir, queries, records, candidates, manifest)
                print(f"Query {idx} failed: {row['error']}", file=sys.stderr, flush=True)
        await context.close()
        await browser.close()
    progress = save_run(run_dir, queries, records, candidates, manifest, blocked=blocked)
    print(json.dumps(progress, indent=2))
    if blocked or any(records.get(q["query_index"], {}).get("terminal_status") in {"error", "no_results"} for q in batch):
        return 2 if blocked else 1
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--categories", required=True, help="One category per line")
    parser.add_argument("--locations", required=True, help="One named location per line")
    parser.add_argument("--region", required=True)
    parser.add_argument("--state-dir", required=True, type=Path, help="Persistent run directory inside the repository")
    parser.add_argument("--max-queries", type=int, default=3, help="Maximum pending queries this invocation processes (1-20)")
    parser.add_argument("--max-sites-per-query", type=int, default=3, help="Maximum direct websites checked per query (1-8)")
    return parser.parse_args()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(run_batch(parse_args())))
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
