#!/usr/bin/env python3
"""Resumable local-business website discovery and evidence capture for GitHub Actions."""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import ipaddress
import json
import re
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
if __package__:
    from .site_audit import PageObserver, load_lazy_images, run_site_checks
    from .page_flow import inspect_page_flow
else:  # Supports the workflow's direct `python tools/lead_hunt.py` invocation.
    from site_audit import PageObserver, load_lazy_images, run_site_checks
    from page_flow import inspect_page_flow



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


def observed_url(url: str) -> dict:
    """Return URL metadata without query strings, fragments, or credentials."""
    try:
        parts = urlsplit(url)
        return {"scheme": parts.scheme.lower(), "host": (parts.hostname or "").lower(),
                "path": parts.path[:300]}
    except ValueError:
        return {"scheme": "", "host": "", "path": ""}


class BehaviorLogger:
    """Append-only diagnostic events; never writes request headers or page bodies."""
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.started = time.monotonic()
        self.sequence = 0
        self.stream = path.open("a", encoding="utf-8", newline="\n")
        self.emit("batch_process_started", log_file=path.name)

    def emit(self, event: str, **fields) -> None:
        self.sequence += 1
        record = {"sequence": self.sequence,
                  "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                  "elapsed_ms": round((time.monotonic() - self.started) * 1000),
                  "event": event, **fields}
        self.stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.stream.flush()

    def close(self) -> None:
        if not self.stream.closed:
            self.emit("batch_process_finished")
            self.stream.close()


def observe_ddg_requests(page, logger: BehaviorLogger) -> None:
    """Capture DDG request metadata only; omit query values, headers, cookies, and bodies."""
    def is_ddg(url: str) -> bool:
        host = observed_url(url)["host"]
        return host == "duckduckgo.com" or host.endswith(".duckduckgo.com")

    def on_request(request) -> None:
        if not is_ddg(request.url):
            return
        logger.emit("ddg_request_started", url=observed_url(request.url), method=request.method,
                    resource_type=request.resource_type, navigation=request.is_navigation_request())

    def on_response(response) -> None:
        if not is_ddg(response.url):
            return
        headers = response.headers
        logger.emit("ddg_response_received", url=observed_url(response.url), status=response.status,
                    resource_type=response.request.resource_type,
                    navigation=response.request.is_navigation_request(),
                    content_type=headers.get("content-type"), retry_after=headers.get("retry-after"))

    def on_request_failed(request) -> None:
        if not is_ddg(request.url):
            return
        failure = getattr(request, "failure", None)
        logger.emit("ddg_request_failed", url=observed_url(request.url), method=request.method,
                    resource_type=request.resource_type, failure=str(failure or "")[:250])

    page.on("request", on_request)
    page.on("response", on_response)
    page.on("requestfailed", on_request_failed)


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")[:90] or "item"


def split_values(value: str) -> list[str]:
    values = [line.strip() for line in re.split(r"[\r\n]+", value) if line.strip()]
    if not values:
        raise ValueError("At least one non-empty value is required")
    if len({v.casefold() for v in values}) != len(values):
        raise ValueError("Input contains duplicate values")
    return values


def read_csv_values(path: Path, column: str) -> list[str]:
    """Read an ordered, unique list from a single named CSV column."""
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or column not in reader.fieldnames:
            raise ValueError(f"{path} must contain a {column!r} column")
        values = [(row.get(column) or "").strip() for row in reader]
    values = [value for value in values if value]
    if not values:
        raise ValueError(f"{path} contains no non-empty values in column {column!r}")
    if len({value.casefold() for value in values}) != len(values):
        raise ValueError(f"{path} contains duplicate values in column {column!r}")
    return values


def resolved_inputs(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    if args.categories_csv or args.cities_csv:
        if not args.categories_csv or not args.cities_csv:
            raise ValueError("Both --categories-csv and --cities-csv are required together")
        categories = read_csv_values(args.categories_csv, "category")
        locations = read_csv_values(args.cities_csv, "city")
        return categories, locations
    if not args.categories or not args.locations:
        raise ValueError("Provide both --categories/--locations or both CSV files")
    return split_values(args.categories), split_values(args.locations)


def csv_input_metadata(path: Path, column: str) -> dict:
    return {"path": str(path.as_posix()), "column": column,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


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
    technical_priority = "uncertain" if not enough else "high" if score >= 61 else "medium" if score >= 41 else "pass"
    email_signal = ((metrics.get("site_checks") or {}).get("contact_email_opportunity") or {})
    # The email signal is an opportunity bonus, separate from the technical
    # priority band. Apply it to the normalized score whenever the assessment
    # has enough evidence; the resulting score then determines the final band.
    email_bonus = 5 if (enough and email_signal.get("consumer_provider_domain_mismatch")) else 0
    if score is not None and email_bonus:
        score = min(100, score + email_bonus)
    priority = "uncertain" if not enough else "high" if score >= 61 else "medium" if score >= 41 else "pass"
    evidence = json.loads(json.dumps(metrics))
    scored_email_signal = ((evidence.get("site_checks") or {}).get("contact_email_opportunity") or {})
    if scored_email_signal:
        scored_email_signal["potential_opportunity_bonus_points"] = 5 if email_signal.get("consumer_provider_domain_mismatch") else 0
        scored_email_signal["opportunity_bonus_points"] = email_bonus
        scored_email_signal["bonus_status"] = "applied" if email_bonus else (
            "not_applied_insufficient_evidence" if email_signal.get("consumer_provider_domain_mismatch") and not enough
            else "not_detected")
    desktop_hero = desktop.get("heroMedia") or {} if isinstance(desktop, dict) else {}
    mobile_hero = mobile.get("heroMedia") or {} if isinstance(mobile, dict) else {}
    hero_statuses = [view.get("status") for view in (desktop_hero, mobile_hero) if isinstance(view, dict)]
    hero_trigger_views = [name for name, view in (("desktop", desktop_hero), ("mobile", mobile_hero))
                         if isinstance(view, dict) and view.get("status") in {"absent", "uncertain"}]
    overall_hero_status = ("uncertain" if "uncertain" in hero_statuses else
                           "absent" if "absent" in hero_statuses else
                           "present" if "present" in hero_statuses else "uncertain")
    hero_media = {"status": overall_hero_status, "trigger_views": hero_trigger_views,
                  "desktop": desktop_hero, "mobile": mobile_hero}
    return {"automated_priority": priority, "website_opportunity_score": score,
            "technical_priority_before_email_bonus": technical_priority,
            "email_opportunity_bonus_points": email_bonus,
            "score_basis": "normalized technical score plus 5 opportunity points for a detected consumer email domain when evidence is sufficient; absent or uncertain desktop/mobile first-viewport hero media triggers visual review",
            "signals": {key: "unknown" if value is None else "issue" if value else "clear" for key, value in checks.items()},
            "opportunity_signals": {"consumer_email_domain_mismatch":
                                    "observed" if email_signal.get("consumer_provider_domain_mismatch") else
                                    "not_observed" if "consumer_provider_domain_mismatch" in email_signal else "unknown"},
            "hero_media": hero_media,
            "visual_review_trigger": hero_media.get("status") in {"absent", "uncertain"},
            "evidence": evidence, "evidence_confidence": "sufficient" if enough else "limited"}


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


def fingerprint(queries: list[dict], region: str, engine: str = "chatgpt_web_ui") -> str:
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


async def screenshot_with_fallback(page, path: Path, full_timeout_ms: int = 30000,
                                   logger: BehaviorLogger | None = None, stage: str = "unspecified") -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    errors = []
    budgets = [min(20000, full_timeout_ms), max(1000, full_timeout_ms - min(20000, full_timeout_ms))]
    for attempt, timeout_ms in enumerate(budgets, start=1):
        started = time.monotonic()
        if logger:
            logger.emit("screenshot_attempt_started", stage=stage, filename=path.name,
                        mode="full_page", attempt=attempt, timeout_ms=timeout_ms)
        try:
            await page.screenshot(path=str(path), full_page=True, animations="disabled", timeout=timeout_ms)
            saved_path = path.resolve()
            try:
                display_path = saved_path.relative_to(Path.cwd().resolve()).as_posix()
            except ValueError:
                display_path = saved_path.as_posix()
            result = {"path": display_path, "mode": "full_page", "status": "saved",
                      "attempts": attempt, "prior_errors": errors}
            if logger:
                logger.emit("screenshot_saved", stage=stage, filename=path.name, mode="full_page",
                            attempt=attempt, elapsed_ms=round((time.monotonic() - started) * 1000))
            return result
        except Exception as exc:
            errors.append(str(exc)[:400])
            if logger:
                logger.emit("screenshot_attempt_failed", stage=stage, filename=path.name,
                            mode="full_page", attempt=attempt, timeout_ms=timeout_ms,
                            exception_type=type(exc).__name__, elapsed_ms=round((time.monotonic() - started) * 1000))
            if attempt == 1:
                await page.wait_for_timeout(1000)
    fallback_path = path.with_name(path.stem + "_viewport" + path.suffix)
    started = time.monotonic()
    if logger:
        logger.emit("screenshot_attempt_started", stage=stage, filename=fallback_path.name,
                    mode="viewport_fallback", attempt=3, timeout_ms=15000)
    try:
        await page.screenshot(path=str(fallback_path), full_page=False, animations="disabled", timeout=15000)
        saved_path = fallback_path.resolve()
        try:
            display_path = saved_path.relative_to(Path.cwd().resolve()).as_posix()
        except ValueError:
            display_path = saved_path.as_posix()
        result = {"path": display_path, "mode": "viewport_fallback", "status": "saved",
                  "attempts": 3, "full_page_errors": errors}
        if logger:
            logger.emit("screenshot_saved", stage=stage, filename=fallback_path.name,
                        mode="viewport_fallback", attempt=3,
                        elapsed_ms=round((time.monotonic() - started) * 1000))
        return result
    except Exception as view_error:
        if logger:
            logger.emit("screenshot_attempt_failed", stage=stage, filename=fallback_path.name,
                        mode="viewport_fallback", attempt=3, timeout_ms=15000,
                        exception_type=type(view_error).__name__,
                        elapsed_ms=round((time.monotonic() - started) * 1000))
        return {"path": None, "mode": "none", "status": "error",
                "full_page_errors": errors, "viewport_error": str(view_error)[:400]}


async def page_metrics(page) -> dict:
    return await page.evaluate("""() => {
      const text = document.body?.innerText || '';
      const viewport = {width: window.innerWidth, height: window.innerHeight};
      const bounds = el => {
        const r = el.getBoundingClientRect();
        const left = Math.max(0, r.left), top = Math.max(0, r.top);
        const right = Math.min(viewport.width, r.right), bottom = Math.min(viewport.height, r.bottom);
        return {x: Math.round(r.left), y: Math.round(r.top), width: Math.round(r.width),
          height: Math.round(r.height), visibleWidth: Math.max(0, Math.round(right-left)),
          visibleHeight: Math.max(0, Math.round(bottom-top))};
      };
      const visible = el => {
        if (!el || !el.getClientRects().length) return false;
        const s = getComputedStyle(el), r = el.getBoundingClientRect();
        return s.visibility !== 'hidden' && Number(s.opacity || 1) > 0.05 &&
          r.width > 0 && r.height > 0 && r.right > 0 && r.bottom > 0 &&
          r.left < viewport.width && r.top < viewport.height;
      };
      const relativeTo = (rect, other) => {
        if (!other) return 'not_found';
        const xOverlap = rect.x < other.x + other.width && other.x < rect.x + rect.width;
        const yOverlap = rect.y < other.y + other.height && other.y < rect.y + rect.height;
        if (xOverlap && yOverlap) return 'overlaps';
        if (rect.y + rect.height <= other.y) return 'above';
        if (rect.y >= other.y + other.height) return 'below';
        return rect.x + rect.width <= other.x ? 'left' : rect.x >= other.x + other.width ? 'right' : 'adjacent';
      };
      const headline = [...document.querySelectorAll('h1')].find(visible) || null;
      const cta = [...document.querySelectorAll('a[href],button,[role="button"]')].find(el =>
        visible(el) && /contact|call|book|schedule|request|get a quote|estimate|get in touch/i.test((el.innerText || el.textContent || '').trim()));
      const headlineRect = headline ? bounds(headline) : null;
      const ctaRect = cta ? bounds(cta) : null;
      const media = [];
      const pushMedia = (el, type, naturalWidth=0, naturalHeight=0, alt='') => {
        const r = bounds(el), area = r.visibleWidth * r.visibleHeight;
        if (!area) return;
        const areaPercent = Math.round(area * 1000 / Math.max(1, viewport.width * viewport.height)) / 10;
        const loaded = type === 'image' ? naturalWidth > 0 : type === 'video' ? naturalWidth > 0 : null;
        media.push({type, x:r.x, y:r.y, width:r.width, height:r.height,
          visibleWidth:r.visibleWidth, visibleHeight:r.visibleHeight, viewportAreaPercent:areaPercent,
          naturalWidth, naturalHeight, loaded, alt:(alt || '').trim().slice(0,180),
          relation_to_headline:relativeTo(r, headlineRect), relation_to_primary_cta:relativeTo(r, ctaRect),
          prominent:r.visibleWidth >= 120 && r.visibleHeight >= 90 && areaPercent >= 8 && loaded !== false});
      };
      for (const img of document.querySelectorAll('img')) if (visible(img))
        pushMedia(img, 'image', img.naturalWidth || 0, img.naturalHeight || 0, img.alt || '');
      for (const video of document.querySelectorAll('video')) if (visible(video))
        pushMedia(video, 'video', video.videoWidth || 0, video.videoHeight || 0, video.getAttribute('aria-label') || '');
      for (const el of [document.body, ...document.querySelectorAll('body *')].slice(0, 1600)) {
        if (!visible(el)) continue;
        const background = getComputedStyle(el).backgroundImage;
        if (background && background !== 'none' && /url\\s*\\(/i.test(background))
          pushMedia(el, 'background_image');
      }
      const prominentCount = media.filter(item => item.prominent).length;
      const heroMedia = {status: !headline ? 'uncertain' : prominentCount ? 'present' : 'absent',
        viewport, headline: headline ? {text:(headline.innerText || headline.textContent || '').trim().slice(0,240), ...headlineRect} : null,
        primary_cta: cta ? {text:(cta.innerText || cta.textContent || '').trim().slice(0,180), ...ctaRect} : null,
        media_candidates: media.slice(0,30), prominent_media_count:prominentCount,
        detection_note: !headline ? 'No visible first-viewport h1; hero boundary is uncertain.' :
          prominentCount ? 'At least one prominent image, video, or CSS background image is visible in the first viewport.' :
          'No prominent image, video, or CSS background image was detected in the first viewport.'};
      return {
        viewportMeta: !!document.querySelector('meta[name="viewport"]'),
        documentWidth: document.documentElement.scrollWidth,
        viewportWidth: document.documentElement.clientWidth,
        horizontalOverflowPx: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth),
        imageCount: [...document.images].length,
        loadedImageCount: [...document.images].filter(i => i.complete && i.naturalWidth > 0).length,
        hasContactPath: [...document.querySelectorAll('a[href]')].some(a => /contact|location|visit|call|phone/i.test((a.innerText||'')+' '+a.href)),
        hasCta: [...document.querySelectorAll('a,button')].some(a => /contact|call|visit|book|schedule|request|get a quote|estimate|get in touch/i.test((a.innerText||'').trim())),
        titlePresent: !!document.title.trim(),
        descriptionPresent: !!document.querySelector('meta[name="description"]')?.content.trim(),
        copyrightYears: [...text.matchAll(/(?:©|copyright)\\s*(?:\\d{4}\\s*[-–]\\s*)?(20\\d{2})/ig)].map(m => Number(m[1])),
        heroMedia
      };
    }""")


async def assess_site(page, result: dict, query: dict, run_dir: Path,
                      public_host_cache: dict[str, bool], logger: BehaviorLogger | None = None) -> dict:
    url = result["url"]
    requested_host = result["domain"]
    if requested_host not in public_host_cache:
        public_host_cache[requested_host] = await asyncio.to_thread(public_host, requested_host)
    if not public_host_cache[requested_host]:
        return {"site_check_status": "blocked_non_public_host", "domain": requested_host,
                "reason": "Host is local, private, or did not resolve exclusively to public IP addresses"}
    metrics: dict = {"requested_https": urlsplit(url).scheme == "https"}
    shots: dict = {}
    observer = PageObserver(page)
    try:
        await page.set_viewport_size({"width": 1440, "height": 1000})
        started = time.monotonic()
        if logger:
            logger.emit("direct_site_navigation_started", query_index=query.get("query_index"),
                        result_position=result.get("position"), domain=requested_host)
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        except Exception as first_navigation_error:
            if "ERR_ABORTED" not in str(first_navigation_error):
                raise
            if logger:
                logger.emit("direct_site_navigation_retry", query_index=query.get("query_index"),
                            domain=requested_host, reason="ERR_ABORTED", wait_ms=1000)
            await page.wait_for_timeout(1000)
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
        metrics["https"] = urlsplit(final_url).scheme.lower() == "https"
        metrics["title"] = await page.title()
        image_loading = await load_lazy_images(page)
        metrics["desktop"] = await page_metrics(page)
        metrics["desktop"]["pageFlow"] = await inspect_page_flow(page)
        if logger:
            logger.emit("direct_site_navigation_completed", query_index=query.get("query_index"),
                        domain=requested_host, final_domain=final_host,
                        status=response.status if response else None,
                        elapsed_ms=round((time.monotonic() - started) * 1000))
        stem = slugify(final_host)
        desktop = await screenshot_with_fallback(page, run_dir / "screenshots" / f"{stem}_desktop.png",
                                                 logger=logger, stage="desktop")
        shots["desktop"] = desktop

        await page.set_viewport_size({"width": 390, "height": 844})
        await page.wait_for_timeout(350)
        metrics["mobile"] = await page_metrics(page)
        metrics["mobile"]["pageFlow"] = await inspect_page_flow(page)
        mobile = await screenshot_with_fallback(page, run_dir / "screenshots" / f"{stem}_mobile.png", 25000,
                                                logger=logger, stage="mobile")
        shots["mobile"] = mobile
        try:
            metrics["site_checks"] = await run_site_checks(
                page, final_url, observer, public_host_cache, public_host, image_loading)
        except Exception as audit_error:
            metrics["site_checks"] = {"status": "unknown",
                                      "error": f"{type(audit_error).__name__}: {audit_error}"[:500]}
        signals = score_signals(metrics)
        return {"site_check_status": "loaded", "domain": final_host, "final_url": final_url,
                "site_title": metrics.get("title", ""), "automated_priority": signals["automated_priority"],
                "website_opportunity_score": signals["website_opportunity_score"],
                "hero_media": signals["hero_media"],
                "visual_review_trigger": signals["visual_review_trigger"],
                "score_basis": signals["score_basis"], "signals": signals["signals"],
                "technical_priority_before_email_bonus": signals["technical_priority_before_email_bonus"],
                "email_opportunity_bonus_points": signals["email_opportunity_bonus_points"],
                "opportunity_signals": signals["opportunity_signals"],
                "evidence": signals["evidence"], "screenshots": shots, "evidence_confidence": signals["evidence_confidence"],
                "site_checks": (signals.get("evidence") or {}).get("site_checks", {"status": "unknown"}),
                "hermes_visual_review": "pending", "human_review": "pending"}
    except Exception as exc:
        if logger:
            logger.emit("direct_site_assessment_failed", query_index=query.get("query_index"),
                        domain=requested_host, exception_type=type(exc).__name__,
                        err_aborted="ERR_ABORTED" in str(exc))
        metrics["assessment_error"] = f"{type(exc).__name__}: {exc}"[:700]
        signals = score_signals(metrics)
        return {"site_check_status": "error", "domain": host_of(page.url) or requested_host,
                "final_url": page.url, "automated_priority": signals["automated_priority"],
                "website_opportunity_score": signals["website_opportunity_score"],
                "hero_media": signals["hero_media"],
                "visual_review_trigger": signals["visual_review_trigger"],
                "score_basis": signals["score_basis"], "signals": signals["signals"],
                "technical_priority_before_email_bonus": signals["technical_priority_before_email_bonus"],
                "email_opportunity_bonus_points": signals["email_opportunity_bonus_points"],
                "opportunity_signals": signals["opportunity_signals"],
                "evidence": metrics, "screenshots": shots,
                "evidence_confidence": "limited", "hermes_visual_review": "pending",
                "human_review": "pending"}
    finally:
        observer.close()


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


CHATGPT_INSTRUCTIONS = """You are researching local businesses for a human-reviewed website assessment.
Use ChatGPT web search to identify independent local businesses that match the category and
serve the specified location. Return direct official business website URLs, never directory,
marketplace, social-media, or aggregator URLs. Include the source URLs that support the business
identity, official website, and service area. Do not invent names, URLs, citations, or evidence.
Return only one valid JSON object matching this schema:
{"businesses":[{"business_name":"...","official_website_url":"https://...",
"service_area_evidence":"...","source_urls":["https://..."]}],"limitations":"..."}
If none can be verified, return {"businesses":[],"limitations":"..."}."""


def build_chatgpt_prompt(query: dict, limit: int) -> str:
    return (f"Find up to {limit} businesses for this search.\n"
            f"Category: {query['category']}\nLocation: {query['location']}\n"
            "Use web search and prefer independent businesses serving this exact area. "
            "For every business, cite source URLs and explain the service-area evidence. "
            "Return only the JSON object specified in the instructions.")


async def capture_google_search_evidence(page, query: dict, run_dir: Path,
                                         logger: BehaviorLogger) -> dict:
    """Capture the first Google results page for controlled comparison; do not handle challenges."""
    index = int(query["query_index"])
    evidence_dir = run_dir / "search_evidence" / "google"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    search_url = "https://www.google.com/search?" + urlencode({"q": query["query"], "num": "10"})
    row = {"query_index": index, "exact_query": query["query"], "requested_url": search_url,
           "captured_at": now_iso(), "terminal_status": "in_progress",
           "organic_results_reported": None, "organic_results_returned_count": 0,
           "organic_results_inspected_count": 0, "organic_results": []}
    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1500)
        row["actual_url"] = page.url
        row["page_title"] = await page.title()
        body = await page.locator("body").inner_text(timeout=10000)
        lower_body = body.lower()
        blocked = ("/sorry/" in page.url.lower() or any(term in lower_body for term in (
            "unusual traffic", "not a robot", "verify you are human", "automated queries")))
        row["challenge_status"] = "blocked" if blocked else "clear"
        count = re.search(r"(?:About\s+)?([\d,]+)\s+results", body, re.IGNORECASE)
        if count:
            row["organic_results_reported"] = int(count.group(1).replace(",", ""))
        if blocked:
            row["terminal_status"] = "blocked"
        else:
            raw_results = await page.evaluate("""() => {
              const root = document.querySelector('#rso') || document.querySelector('#search') || document.body;
              const primary = [...root.querySelectorAll('.tF2Cxc')];
              const cards = primary.length ? primary : [...root.querySelectorAll('.MjjYud')];
              return cards.map(box => {
                const link = box.querySelector('a:has(h3)');
                const heading = box.querySelector('h3');
                if (!link || !heading) return null;
                return {url: link.href, title: (heading.innerText || '').trim(),
                        context: (box.innerText || '').trim()};
              }).filter(item => item && /^https?:/.test(item.url));
            }""")
            seen: set[str] = set()
            for item in raw_results:
                url = str(item.get("url") or "")
                host = host_of(url)
                if not host or host == "google.com" or host.endswith(".google.com") or url in seen:
                    continue
                seen.add(url)
                row["organic_results"].append({
                    "position": len(row["organic_results"]) + 1,
                    "title": str(item.get("title") or "")[:250], "url": url,
                    "domain": host, "context": str(item.get("context") or "")[:1200],
                })
                if len(row["organic_results"]) >= 10:
                    break
            row["organic_results_returned_count"] = len(row["organic_results"])
            row["organic_results_inspected_count"] = 0
            row["results_extracted_from_page_count"] = len(row["organic_results"])
            for result in row["organic_results"]:
                result["host_screen_disposition"], result["host_screen_reason"] = classify_result(result)
            row["terminal_status"] = "success" if row["organic_results"] else "no_results"
        row["screenshot"] = await screenshot_with_fallback(
            page, evidence_dir / f"query_{index:04d}_serp.png", 20000, logger, "google_serp"
        )
    except Exception as exc:
        row.update({"terminal_status": "error", "error": f"{type(exc).__name__}: {exc}"[:1000],
                    "actual_url": page.url})
    logger.emit("google_evidence_captured", query_index=index, status=row["terminal_status"],
                result_count=row["organic_results_returned_count"], challenge=row.get("challenge_status"))
    return row


def run_chatgpt_discovery(query: dict, run_dir: Path, logger: BehaviorLogger,
                          result_limit: int) -> tuple[list[dict], dict]:
    """Run the ChatGPT browser runner under the workflow's Xvfb display."""
    index = int(query["query_index"])
    query_dir = run_dir / "chatgpt" / f"query_{index:04d}"
    query_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = query_dir / "prompt.txt"
    prompt_file.write_text(build_chatgpt_prompt(query, result_limit) + "\n", encoding="utf-8")
    instructions_file = query_dir / "instructions.txt"
    instructions_file.write_text(CHATGPT_INSTRUCTIONS + "\n", encoding="utf-8")
    runner = Path(__file__).resolve().with_name("chatgpt_file_playwright_anonymous_ci.py")
    results: list[dict] = []
    attempt_records = []
    last_error = ""

    for attempt in (1, 2):
        attempt_dir = query_dir / f"attempt_{attempt:02d}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        output_file = attempt_dir / "response.md"
        parsed_file = attempt_dir / "response.json"
        metadata_file = attempt_dir / "metadata.json"
        screenshot_file = attempt_dir / "chatgpt-response.png"
        error_screenshot = attempt_dir / "playwright-error.png"
        error_html = attempt_dir / "playwright-error.html"
        with tempfile.TemporaryDirectory(prefix=f"chatgpt-profile-{index:04d}-") as profile:
            command = [
                sys.executable, str(runner),
                "--stage-name", f"lead_hunt_query_{index:04d}",
                "--input-file", str(prompt_file),
                "--instructions-file", str(instructions_file),
                "--output-file", str(output_file),
                "--parsed-output-file", str(parsed_file),
                "--metadata-output-file", str(metadata_file),
                "--expect-json", "--fail-if-invalid-json",
                "--user-data-dir", profile,
                "--allow-retry", "--refresh-before-retry",
                "--wait-seconds", "70",
                "--timeout-ms", "120000",
                "--post-response-wait-seconds", "2",
                "--response-settle-seconds", "2",
                "--max-settle-wait-seconds", "30",
                "--error-screenshot", str(error_screenshot),
                "--success-screenshot", str(screenshot_file),
            ]
            if attempt > 1:
                command.append("--refresh-before-send")
            started = time.monotonic()
            completed = subprocess.run(command, capture_output=True, text=True, timeout=420)
            duration_ms = round((time.monotonic() - started) * 1000)
        attempt_record = {"attempt": attempt, "exit_code": completed.returncode,
                          "duration_ms": duration_ms,
                          "response_file": str(output_file.relative_to(run_dir)).replace("\\", "/"),
                          "screenshot": str(screenshot_file.relative_to(run_dir)).replace("\\", "/")
                          if screenshot_file.exists() else None,
                          "error_screenshot": str(error_screenshot.relative_to(run_dir)).replace("\\", "/")
                          if error_screenshot.exists() else None,
                          "error_html": str(error_html.relative_to(run_dir)).replace("\\", "/")
                          if error_html.exists() else None}
        if completed.returncode == 0 and parsed_file.exists():
            payload = json.loads(parsed_file.read_text(encoding="utf-8"))
            businesses = payload.get("businesses", payload.get("results", payload.get("candidates", [])))
            if not isinstance(businesses, list):
                raise ValueError("ChatGPT JSON must contain a businesses array")
            for item in businesses[:result_limit]:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("business_name") or item.get("name") or "").strip()[:300]
                raw_url = str(item.get("official_website_url") or item.get("website_url") or item.get("url") or "").strip()
                normalized = resolve_ddg_result(raw_url)
                sources_value = item.get("source_urls") or item.get("sources") or []
                if isinstance(sources_value, str):
                    sources_value = [sources_value]
                sources = []
                if isinstance(sources_value, list):
                    for value in sources_value:
                        source_url = str(value.get("url") or value.get("source_url") or "").strip() if isinstance(value, dict) else str(value).strip()
                        if source_url:
                            sources.append(source_url)
                name = name or normalized.get("domain", "")
                results.append({
                    "position": len(results) + 1,
                    "title": name,
                    "snippet": str(item.get("service_area_evidence") or item.get("evidence") or "").strip()[:1200],
                    "raw_url": raw_url,
                    "url": normalized.get("url"),
                    "domain": normalized.get("domain", ""),
                    "source_urls": sources,
                    "service_area_evidence": str(item.get("service_area_evidence") or item.get("evidence") or "").strip()[:1200],
                    "result_disposition": None,
                    "disposition_reason": None,
                })
            attempt_record["parsed_business_count"] = len(results)
            attempt_records.append(attempt_record)
            logger.emit("chatgpt_discovery_completed", query_index=index, attempt=attempt,
                        candidate_count=len(results), duration_ms=duration_ms,
                        response_file=attempt_record["response_file"], screenshot=attempt_record["screenshot"])
            metadata = json.loads(metadata_file.read_text(encoding="utf-8")) if metadata_file.exists() else {}
            return results, {"attempt_history": attempt_records, "attempt": attempt,
                             "prompt_file": str(prompt_file.relative_to(run_dir)).replace("\\", "/"),
                             "instructions_file": str(instructions_file.relative_to(run_dir)).replace("\\", "/"),
                             "response_file": attempt_record["response_file"],
                             "response_json_file": str(parsed_file.relative_to(run_dir)).replace("\\", "/"),
                             "response_screenshot": attempt_record["screenshot"],
                             "runner_metadata_file": str(metadata_file.relative_to(run_dir)).replace("\\", "/"),
                             "runner_metadata": metadata,
                             "chatgpt_limitations": str(payload.get("limitations") or "")[:1200]}
        last_error = ((completed.stderr or completed.stdout or f"runner exit code {completed.returncode}").strip())[-2000:]
        attempt_record["error"] = last_error
        attempt_records.append(attempt_record)
        logger.emit("chatgpt_discovery_attempt_failed", query_index=index, attempt=attempt,
                    exit_code=completed.returncode, duration_ms=duration_ms, error=last_error[:500],
                    error_screenshot=attempt_record["error_screenshot"])

    raise RuntimeError(json.dumps({"error": last_error, "attempt_history": attempt_records}, ensure_ascii=False))


async def run_chatgpt_batch(args: argparse.Namespace) -> int:
    from playwright.async_api import async_playwright

    run_dir = args.state_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    categories, locations = resolved_inputs(args)
    queries = build_queries("\n".join(categories), "\n".join(locations), args.region)
    if not 1 <= args.max_queries <= 20:
        raise ValueError("max-queries must be between 1 and 20")
    if not 1 <= args.max_sites_per_query <= 10:
        raise ValueError("max-sites-per-query must be between 1 and 10")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", run_dir.name):
        raise ValueError("state-dir folder name must be a safe run key (letters, digits, dot, underscore, hyphen)")
    manifest_path = run_dir / "RUN_MANIFEST.json"
    input_sources = ({"categories": csv_input_metadata(args.categories_csv, "category"),
                      "cities": csv_input_metadata(args.cities_csv, "city")}
                     if args.categories_csv and args.cities_csv else {})
    manifest = {"run_key": run_dir.name, "region": args.region, "engine": "chatgpt_web_ui",
                "categories": categories, "locations": locations,
                "configured_queries": len(queries), "input_fingerprint": fingerprint(queries, args.region, "chatgpt_web_ui"),
                "input_sources": input_sources,
                "google_evidence_enabled": bool(args.capture_google_evidence),
                "commit_screenshots_enabled": False,
                "workflow": "GitHub Actions Playwright headed Chromium under Xvfb; ChatGPT response runner",
                "created_at": now_iso(), "updated_at": now_iso(), "automated_scoring_is_final": False,
                "review_policy": "ChatGPT discovery evidence, direct-site signals, Hermes visual review, and human decisions remain separate"}
    if manifest_path.exists():
        prior = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        if prior.get("input_fingerprint") != manifest["input_fingerprint"]:
            raise ValueError("This run key already contains different categories/locations/region; choose a new run key")
        manifest = {**prior, "updated_at": now_iso(),
                    "google_evidence_enabled": bool(args.capture_google_evidence),
                    "commit_screenshots_enabled": False}
    logger = BehaviorLogger(run_dir / "BEHAVIOR_LOG.jsonl")
    records = {int(row["query_index"]): row for row in read_jsonl(run_dir / "QUERY_LEDGER.jsonl")
               if row.get("query_index") is not None}
    google_records = {int(row["query_index"]): row for row in read_jsonl(run_dir / "GOOGLE_SEARCH_LEDGER.jsonl")
                      if row.get("query_index") is not None}
    candidates = read_jsonl(run_dir / "CANDIDATE_LEDGER.jsonl")
    candidate_by_domain = {row.get("domain", "").lower(): row for row in candidates if row.get("domain")}
    exclusions = set()
    exclusion_path = Path(__file__).resolve().parents[1] / "config" / "excluded_domains.txt"
    if exclusion_path.exists():
        exclusions = {line.strip().lower().removeprefix("www.") for line in exclusion_path.read_text(encoding="utf-8").splitlines()
                      if line.strip() and not line.strip().startswith("#")}
    public_host_cache: dict[str, bool] = {}
    pending = [query for query in queries
               if not query_is_complete(records.get(query["query_index"], {})) or
               (args.capture_google_evidence and query["query_index"] not in google_records)]
    batch = pending[:args.max_queries]
    logger.emit("batch_configuration", configured_query_count=len(queries), pending_query_count=len(pending),
                selected_query_count=len(batch), max_queries=args.max_queries,
                max_sites_per_query=args.max_sites_per_query, search_engine="chatgpt_web_ui",
                browser_runner="ChatGPT Playwright runner", display="Xvfb")
    if not batch:
        progress = save_run(run_dir, queries, records, candidates, manifest)
        logger.emit("batch_no_pending_queries")
        logger.close()
        print(json.dumps(progress, indent=2))
        return 0

    blocked = False
    try:
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
            logger.emit("browser_ready", browser="playwright_chromium_headed", display="xvfb",
                        site_navigation="direct URL after ChatGPT extraction", result_link_clicks=0)
            for query in batch:
                idx = query["query_index"]
                if args.capture_google_evidence and idx not in google_records:
                    google_records[idx] = await capture_google_search_evidence(page, query, run_dir, logger)
                    atomic_jsonl(run_dir / "GOOGLE_SEARCH_LEDGER.jsonl",
                                 [google_records[key] for key in sorted(google_records)])
                if query_is_complete(records.get(idx, {})):
                    continue
                row = {"query_index": idx, **query, "search_engine": "chatgpt_web_ui",
                       "terminal_status": "in_progress", "organic_results_returned_count": 0,
                       "organic_results_inspected_count": 0, "organic_results": [],
                       "started_at": now_iso(), "timestamp": now_iso()}
                records[idx] = row
                save_run(run_dir, queries, records, candidates, manifest)
                logger.emit("chatgpt_discovery_started", query_index=idx, category=query["category"], location=query["location"])
                try:
                    results, chatgpt_evidence = await asyncio.to_thread(
                        run_chatgpt_discovery, query, run_dir, logger, max(args.max_sites_per_query * 2, 10))
                    row.update(chatgpt_evidence)
                    row["organic_results_returned_count"] = len(results)
                    if not results:
                        row.update({"terminal_status": "no_results", "finished_at": now_iso(),
                                    "error": "ChatGPT returned valid JSON without any business URLs"})
                        records[idx] = row
                        save_run(run_dir, queries, records, candidates, manifest)
                        continue
                    dispositions = [classify_result(result, exclusions)[0] for result in results]
                    logger.emit("chatgpt_results_extracted", query_index=idx, result_count=len(results),
                                excluded_count=dispositions.count("directory_or_lead_generation"),
                                eligible_count=dispositions.count("eligible_for_site_check"),
                                site_visit_cap=args.max_sites_per_query)
                    inspected = 0
                    eligible_sites = 0
                    for result in results:
                        disposition, reason_text = classify_result(result, exclusions)
                        result["result_disposition"] = disposition
                        result["disposition_reason"] = reason_text
                        logger.emit("chatgpt_result_classified", query_index=idx,
                                    result_position=result["position"], domain=result.get("domain"), disposition=disposition)
                        if disposition != "eligible_for_site_check" or eligible_sites >= args.max_sites_per_query:
                            if disposition == "eligible_for_site_check":
                                result["result_disposition"] = "eligible_not_visited_limit"
                                result["disposition_reason"] = f"Per-query site-check limit ({args.max_sites_per_query}) reached"
                            inspected += 1
                            row.update({"organic_results": results, "organic_results_inspected_count": inspected,
                                        "updated_at": now_iso()})
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
                            site = await assess_site(page, result, query, run_dir, public_host_cache, logger)
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
                                             "source_result_url": (result.get("source_urls") or [result["raw_url"]])[0],
                                             "source_result_title": result["title"],
                                             "source_result_snippet": result["snippet"], "source_result_position": result["position"],
                                             "chatgpt_source_urls": result.get("source_urls", []),
                                             "chatgpt_official_website_url": result.get("raw_url", ""),
                                             "chatgpt_service_area_evidence": result.get("service_area_evidence", ""),
                                             "discovery_method": "chatgpt_web_ui", **site,
                                             "market_evidence": "pending_review", "human_review": "pending",
                                             "hermes_visual_review": "pending", "recorded_at": now_iso()}
                                candidates.append(candidate)
                                candidate_by_domain[candidate["domain"].lower()] = candidate
                                result["candidate_domain"] = candidate["domain"]
                                result["result_disposition"] = "site_assessed"
                                result["disposition_reason"] = "ChatGPT discovery followed by direct-site assessment; market fit and final verdict remain pending"
                        inspected += 1
                        row.update({"organic_results": results, "organic_results_inspected_count": inspected,
                                    "updated_at": now_iso()})
                        records[idx] = row
                        save_run(run_dir, queries, records, candidates, manifest)
                    row.update({"terminal_status": "success", "finished_at": now_iso(),
                                "organic_results": results, "organic_results_inspected_count": inspected})
                    records[idx] = row
                    save_run(run_dir, queries, records, candidates, manifest)
                    logger.emit("chatgpt_query_completed", query_index=idx, result_count=len(results),
                                direct_sites_checked=eligible_sites, status="success")
                except Exception as exc:
                    logger.emit("chatgpt_query_failed", query_index=idx, exception_type=type(exc).__name__)
                    row.update({"terminal_status": "error", "finished_at": now_iso(),
                                "error": f"{type(exc).__name__}: {exc}"[:2500]})
                    records[idx] = row
                    save_run(run_dir, queries, records, candidates, manifest)
                    print(f"ChatGPT query {idx} failed: {row['error']}", file=sys.stderr, flush=True)
            await context.close()
            await browser.close()
    finally:
        logger.close()

    progress = save_run(run_dir, queries, records, candidates, manifest, blocked=blocked)
    print(json.dumps(progress, indent=2))
    return 1 if any(records.get(q["query_index"], {}).get("terminal_status") in {"error", "no_results"} for q in batch) else 0


async def run_batch(args: argparse.Namespace) -> int:
    if args.engine == "chatgpt":
        return await run_chatgpt_batch(args)
    return await run_duckduckgo_batch(args)


async def run_duckduckgo_batch(args: argparse.Namespace) -> int:
    from playwright.async_api import async_playwright

    run_dir = args.state_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    categories, locations = resolved_inputs(args)
    queries = [{"query_index": i, "category_index": ci, "category": category,
                "location_index": li, "location": location, "query": f"{category} {location}"}
               for i, (ci, category, li, location) in enumerate(
                   ((ci, c, li, loc) for ci, c in enumerate(categories, 1)
                    for li, loc in enumerate(locations, 1)), 1)]
    if args.max_queries < 1 or args.max_queries > 20:
        raise ValueError("max-queries must be between 1 and 20")
    if args.max_sites_per_query < 1 or args.max_sites_per_query > 10:
        raise ValueError("max-sites-per-query must be between 1 and 10")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", run_dir.name):
        raise ValueError("state-dir folder name must be a safe run key (letters, digits, dot, underscore, hyphen)")
    input_hash = fingerprint(queries, args.region, "duckduckgo_html")
    manifest_path = run_dir / "RUN_MANIFEST.json"
    input_sources = ({"categories": csv_input_metadata(args.categories_csv, "category"),
                      "cities": csv_input_metadata(args.cities_csv, "city")}
                     if args.categories_csv and args.cities_csv else {})
    manifest = {"run_key": run_dir.name, "region": args.region, "engine": "duckduckgo_html",
                "search_url": DDG_SEARCH_URL, "categories": categories, "locations": locations,
                "input_sources": input_sources,
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
    logger = BehaviorLogger(run_dir / "BEHAVIOR_LOG.jsonl")
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
    logger.emit("batch_configuration", configured_query_count=len(queries), pending_query_count=len(pending),
                selected_query_count=len(batch), max_queries=args.max_queries,
                max_sites_per_query=args.max_sites_per_query, search_engine="duckduckgo_html",
                request_logging="DuckDuckGo host metadata only; query parameters, headers, cookies, and bodies omitted")
    if not batch:
        print("All configured queries are already complete; no search was repeated.")
        progress = save_run(run_dir, queries, records, candidates, manifest)
        print(json.dumps(progress, indent=2))
        logger.emit("batch_no_pending_queries")
        logger.close()
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
        observe_ddg_requests(page, logger)
        logger.emit("browser_ready", browser="playwright_chromium_headed", display="xvfb",
                    page_count=len(context.pages), context_reused_for_direct_sites=True,
                    result_link_clicks=0, site_navigation="direct_page_goto_after_url_extraction")
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
            logger.emit("search_navigation_started", query_index=idx, category=query["category"],
                        location=query["location"], pending_query_ordinal=batch.index(query) + 1,
                        batch_query_count=len(batch), destination=observed_url(search_url))
            search_started = time.monotonic()
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
                logger.emit("search_page_loaded", query_index=idx,
                            response_status=response.status if response else None,
                            destination=observed_url(page.url), title=title[:160], body_character_count=len(body),
                            challenge_status="blocked" if reason else "none_visible",
                            challenge_evidence=reason,
                            navigation_elapsed_ms=round((time.monotonic() - search_started) * 1000))
                serp_path = run_dir / "screenshots" / f"query_{idx:04d}_serp.png"
                serp_shot = await screenshot_with_fallback(page, serp_path, 15000,
                                                           logger=logger, stage="search_results")
                row["serp_screenshot"] = serp_shot
                if reason:
                    logger.emit("search_challenge_detected", query_index=idx,
                                response_status=response.status if response else None,
                                evidence=reason, action="stop_without_retry")
                    row.update({"terminal_status": "blocked", "finished_at": now_iso()})
                    blocked = True
                    save_run(run_dir, queries, records, candidates, manifest, blocked=True)
                    print(f"Search challenge detected; stopped at query {idx}: {reason}", flush=True)
                    break
                results = parse_search_results(await extract_ddg_results(page))
                row["organic_results_returned_count"] = len(results)
                dispositions = [classify_result(result, exclusions)[0] for result in results]
                logger.emit("search_results_extracted", query_index=idx, result_count=len(results),
                            sponsored_count=dispositions.count("sponsored_ad"),
                            excluded_count=dispositions.count("directory_or_lead_generation"),
                            social_count=dispositions.count("social_or_video"),
                            editorial_count=dispositions.count("editorial_or_listicle"),
                            eligible_count=dispositions.count("eligible_for_site_check"),
                            site_visit_cap=args.max_sites_per_query)
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
                    logger.emit("search_result_classified", query_index=idx,
                                result_position=result["position"], domain=result.get("domain"),
                                disposition=disposition)
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
                        site = await assess_site(page, result, query, run_dir, public_host_cache, logger)
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
                logger.emit("search_query_completed", query_index=idx, result_count=len(results),
                            direct_sites_checked=eligible_sites, status="success")
                logger.emit("inter_query_wait_started", query_index=idx, wait_ms=1200)
                await page.wait_for_timeout(1200)
            except Exception as exc:
                logger.emit("search_query_failed", query_index=idx, exception_type=type(exc).__name__,
                            challenge_detection_attempted=bool(row.get("actual_url")))
                row.update({"terminal_status": "error", "finished_at": now_iso(),
                            "error": f"{type(exc).__name__}: {exc}"[:1200]})
                records[idx] = row
                save_run(run_dir, queries, records, candidates, manifest)
                print(f"Query {idx} failed: {row['error']}", file=sys.stderr, flush=True)
        await context.close()
        await browser.close()
    logger.close()
    progress = save_run(run_dir, queries, records, candidates, manifest, blocked=blocked)
    print(json.dumps(progress, indent=2))
    if blocked or any(records.get(q["query_index"], {}).get("terminal_status") in {"error", "no_results"} for q in batch):
        return 2 if blocked else 1
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("chatgpt", "duckduckgo"), default="chatgpt",
                        help="Discovery source (ChatGPT browser is the default)")
    parser.add_argument("--categories", help="One category per line (legacy inline input)")
    parser.add_argument("--locations", help="One named location per line (legacy inline input)")
    parser.add_argument("--categories-csv", type=Path, help="CSV with a 'category' column")
    parser.add_argument("--cities-csv", type=Path, help="CSV with a 'city' column")
    parser.add_argument("--capture-google-evidence", action="store_true",
                        help="Capture the first Google results page for this batch; challenges are recorded, never solved")
    parser.add_argument("--region", required=True)
    parser.add_argument("--state-dir", required=True, type=Path, help="Persistent run directory inside the repository")
    parser.add_argument("--max-queries", type=int, default=3, help="Maximum pending queries this invocation processes (1-20)")
    parser.add_argument("--max-sites-per-query", type=int, default=3, help="Maximum direct websites checked per query (1-10)")
    return parser.parse_args()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(run_batch(parse_args())))
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
