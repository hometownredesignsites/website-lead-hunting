"""Bounded, evidence-first checks for public business websites.

The audit performs read-only HTTP checks and smoke-tests only disclosure/tab
controls. It never submits a contact form or clicks purchase/account actions.
"""
from __future__ import annotations

import asyncio
import html
import json
import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


MAX_LINK_CHECKS = 12
MAX_LAZY_IMAGES = 20
MAX_INTERACTIONS = 8
REQUEST_TIMEOUT_MS = 4500
CONTACT_RE = re.compile(r"contact|contact-us|get-in-touch|reach-us", re.I)
BLOG_RE = re.compile(r"blog|news|journal|articles|stories|updates", re.I)
PROJECT_RE = re.compile(r"portfolio|projects|case-stud|gallery|our-work|recent-work", re.I)
PRIVACY_RE = re.compile(r"privacy", re.I)
DATE_KEYS = {"datepublished", "datemodified", "datecreated", "lastreviewed"}
EMAIL_RE = re.compile(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+", re.I)
CONSUMER_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.ca", "yahoo.co.jp",
    "yahoo.com.au", "ymail.com", "rocketmail.com", "outlook.com", "outlook.co.uk",
    "hotmail.com", "hotmail.co.uk", "live.com", "live.co.uk", "msn.com", "aol.com", "icloud.com", "me.com",
    "mac.com", "proton.me", "protonmail.com", "pm.me", "mail.com", "gmx.com", "gmx.net",
    "fastmail.com", "tutanota.com", "tuta.com", "yandex.com", "yandex.ru", "comcast.net", "att.net", "verizon.net",
}


def _is_http(url: str) -> bool:
    return urlsplit(url or "").scheme.lower() == "http"


def _safe_url(url: str) -> str:
    """Persist scheme/host/path while dropping credentials, query values, and fragments."""
    try:
        parts = urlsplit(url or "")
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = f":{parts.port}" if parts.port else ""
        except ValueError:
            port = ""
        return parts._replace(netloc=host + port, query="", fragment="").geturl()[:600]
    except ValueError:
        return ""


_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+")
_URL_KEYS = {"url", "href", "final_url", "source_url", "page_url", "declared_url", "current_url", "location"}


def _safe_payload(value):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if key.lower() in _URL_KEYS and isinstance(item, str):
                cleaned[key] = _safe_url(item)
            else:
                cleaned[key] = _safe_payload(item)
        return cleaned
    if isinstance(value, list):
        return [_safe_payload(item) for item in value]
    if isinstance(value, str):
        return _URL_IN_TEXT.sub(lambda match: _safe_url(match.group(0).rstrip(".,);")), value)
    return value


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().removeprefix("www.").rstrip(".")
    except ValueError:
        return ""


def _same_site(url: str, base_url: str) -> bool:
    return bool(_host(url)) and _host(url) == _host(base_url)


def _classify_contact_emails(emails: list[str], website_url: str) -> dict:
    website_host = _host(website_url)
    findings, seen = [], set()
    for email in emails:
        email = (email or "").strip().strip(".,;:<>\"'()[]{}")
        if not EMAIL_RE.fullmatch(email):
            continue
        normalized = email.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        domain = normalized.rsplit("@", 1)[1].rstrip(".")
        same_brand_domain = bool(website_host) and (
            domain == website_host or domain.endswith("." + website_host) or website_host.endswith("." + domain))
        provider = next((known for known in sorted(CONSUMER_EMAIL_DOMAINS)
                         if domain == known or domain.endswith("." + known)), None)
        findings.append({"email": normalized, "email_domain": domain,
                         "matches_website_domain": same_brand_domain,
                         "consumer_provider": provider})
    consumer_mismatch = [row for row in findings if row["consumer_provider"] and not row["matches_website_domain"]]
    return {"status": "consumer_provider_found" if consumer_mismatch else "business_domain_found" if findings else "not_found",
            "emails_found": findings[:30], "email_count": len(findings),
            "consumer_provider_domain_mismatch": bool(consumer_mismatch),
            "potential_opportunity_bonus_points": 5 if consumer_mismatch else 0,
            "opportunity_bonus_points": 0,
            "bonus_status": "awaiting_priority_scoring" if consumer_mismatch else "not_detected",
            "scope_note": "Publicly displayed email addresses are a weak small-business clue, not proof of business size or redesign need."}


def _iso_date(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        if len(value) == 4 and value.isdigit():
            return f"{value}-01-01"
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            return None


class PageEvidenceParser(HTMLParser):
    """Extract simple evidence from bounded HTTP responses without running page JS."""
    def __init__(self, page_url: str):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.forms = 0
        self.links: list[dict] = []
        self.privacy_links: list[str] = []
        self.date_candidates: list[dict] = []
        self.blog_links: list[str] = []
        self.project_links: list[str] = []
        self.contact_links: list[str] = []
        self.emails: list[str] = []
        self.last_modified: str | None = None
        self._article_depth = 0
        self._article_links: list[str] = []
        self._article_dates: list[str] = []
        self._script_jsonld = False
        self._script_chunks: list[str] = []
        self._meta: dict[str, str] = {}
        self._anchor: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): (value or "") for key, value in attrs}
        if tag == "form":
            self.forms += 1
        if tag == "article":
            self._article_depth += 1
            if self._article_depth == 1:
                self._article_links, self._article_dates = [], []
        if tag == "a" and values.get("href"):
            href = urljoin(self.page_url, values["href"])
            if href.lower().startswith("mailto:"):
                self.emails.extend(EMAIL_RE.findall(href[7:].split("?", 1)[0]))
            self._anchor = {"href": href, "text": ""}
            if self._article_depth:
                self._article_links.append(href)
            descriptor = f"{href} {values.get('aria-label', '')}"
            if PRIVACY_RE.search(descriptor):
                self.privacy_links.append(href)
            if CONTACT_RE.search(descriptor):
                self.contact_links.append(href)
            if BLOG_RE.search(descriptor):
                self.blog_links.append(href)
            if PROJECT_RE.search(descriptor):
                self.project_links.append(href)
        if tag == "time":
            value = values.get("datetime") or ""
            if value:
                if self._article_depth:
                    self._article_dates.append(value)
                else:
                    self._add_date(value, self.page_url)
        if tag == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content", "")
            if key in {"article:published_time", "article:modified_time", "datepublished", "datemodified", "last-modified", "last-modified-date"} and content:
                self._meta[key] = content
                self._add_date(content, self.page_url, "meta:" + key)
        if tag == "script" and "ld+json" in values.get("type", "").lower():
            self._script_jsonld = True
            self._script_chunks = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor is not None:
            if self._anchor["text"]:
                descriptor = f"{self._anchor['href']} {self._anchor['text']}"
                if PRIVACY_RE.search(descriptor):
                    self.privacy_links.append(self._anchor["href"])
                if CONTACT_RE.search(descriptor):
                    self.contact_links.append(self._anchor["href"])
                if BLOG_RE.search(descriptor):
                    self.blog_links.append(self._anchor["href"])
                if PROJECT_RE.search(descriptor):
                    self.project_links.append(self._anchor["href"])
            self.links.append(self._anchor)
            self._anchor = None
        if tag == "article" and self._article_depth:
            self._article_depth -= 1
            if self._article_depth == 0:
                source = next((u for u in self._article_links if urlsplit(u).path), self.page_url)
                for value in self._article_dates:
                    self._add_date(value, source, "article_time")
                self._article_links, self._article_dates = [], []
        if tag == "script" and self._script_jsonld:
            raw = "".join(self._script_chunks)
            try:
                self._walk_jsonld(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                pass
            self._script_jsonld = False
            self._script_chunks = []

    def handle_data(self, data: str) -> None:
        self.emails.extend(EMAIL_RE.findall(data))
        if self._anchor is not None:
            self._anchor["text"] = (self._anchor["text"] + " " + data).strip()[:300]
        if self._script_jsonld:
            self._script_chunks.append(data)

    def _add_date(self, value: str, source_url: str, kind: str = "time") -> None:
        normalized = _iso_date(value)
        if not normalized:
            try:
                normalized = parsedate_to_datetime(value).date().isoformat()
            except (TypeError, ValueError, OverflowError):
                normalized = None
        if normalized:
            self.date_candidates.append({"date": normalized, "source_url": source_url, "source": kind})

    def _walk_jsonld(self, item, inherited_url: str | None = None) -> None:
        if isinstance(item, list):
            for child in item:
                self._walk_jsonld(child, inherited_url)
        elif isinstance(item, dict):
            source = item.get("url") or item.get("@id") or inherited_url or self.page_url
            main_entity = item.get("mainEntityOfPage")
            if isinstance(main_entity, dict):
                source = main_entity.get("@id") or main_entity.get("url") or source
            if isinstance(source, str):
                source = urljoin(self.page_url, source)
            for key, value in item.items():
                if key.lower() in DATE_KEYS and isinstance(value, str):
                    self._add_date(value, source, "jsonld:" + key)
                elif isinstance(value, (dict, list)):
                    self._walk_jsonld(value, source)


class PageObserver:
    """Collect only public URL/status metadata from the active page."""
    def __init__(self, page):
        self.page = page
        self.responses: list[dict] = []
        self.failures: list[dict] = []
        self.console_errors: list[dict] = []
        self._handlers = []

        def on_response(response):
            request = response.request
            if request.resource_type in {"document", "image", "stylesheet", "script", "font", "media", "fetch", "xhr", "other"}:
                chain = []
                prior = request.redirected_from
                for _ in range(6):
                    if prior is None:
                        break
                    chain.append(prior.url)
                    prior = prior.redirected_from
                self.responses.append({"url": request.url, "final_url": response.url,
                                       "status": response.status, "resource_type": request.resource_type,
                                       "last_modified": response.headers.get("last-modified"),
                                       "redirected": response.url != request.url,
                                       "redirect_chain": list(reversed(chain))[:6]})
                if len(self.responses) > 800:
                    self.responses.pop(0)

        def on_failed(request):
            self.failures.append({"url": request.url, "resource_type": request.resource_type,
                                  "failure": str(request.failure or "request failed")[:250]})
            if len(self.failures) > 300:
                self.failures.pop(0)

        def on_console(message):
            if message.type == "error":
                location = getattr(message, "location", {}) or {}
                self.console_errors.append({"message": message.text[:500],
                                            "source_url": (location.get("url") or "")[:500]})
                if len(self.console_errors) > 100:
                    self.console_errors.pop(0)

        self._handlers = [("response", on_response), ("requestfailed", on_failed), ("console", on_console)]
        for event, handler in self._handlers:
            page.on(event, handler)

    def close(self) -> None:
        for event, handler in self._handlers:
            try:
                self.page.remove_listener(event, handler)
            except Exception:
                pass


async def load_lazy_images(page) -> dict:
    """Scroll a bounded number of lazy images into view before measuring and capturing."""
    try:
        pending = await page.evaluate("""() => [...document.images]
          .map((image, index) => ({index, lazy: image.loading === 'lazy'}))
          .filter(item => item.lazy).map(item => item.index)""")
        attempted = pending[:MAX_LAZY_IMAGES]
        for index in attempted:
            try:
                await page.locator("img").nth(index).scroll_into_view_if_needed(timeout=1800)
                await page.wait_for_timeout(100)
            except Exception:
                continue
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(450)
        return {"attempted": len(attempted), "lazy_images_found": len(pending),
                "complete": len(pending) <= MAX_LAZY_IMAGES}
    except Exception as exc:
        return {"attempted": 0, "lazy_images_found": None, "complete": False,
                "error": f"{type(exc).__name__}: {exc}"[:250]}


async def collect_dom_facts(page) -> dict:
    return await page.evaluate("""() => {
      const visible = el => !!(el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');
      const attrs = el => ({tag: el.tagName.toLowerCase(), src: el.getAttribute('src'),
        href: el.getAttribute('href'), currentSrc: el.currentSrc || '', alt: el.getAttribute('alt') || '',
        loading: el.getAttribute('loading') || '', type: el.getAttribute('type') || '',
        rel: el.getAttribute('rel') || ''});
      const anchors = [...document.querySelectorAll('a[href]')].map(a => ({
        href: a.href, raw_href: a.getAttribute('href') || '', text: (a.innerText || a.getAttribute('aria-label') || '').trim().slice(0, 240),
        target: a.target || '', rel: a.rel || ''})).slice(0, 500);
      const emailPattern = /[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+/ig;
      const emails = new Set((document.body?.innerText || '').match(emailPattern) || []);
      for (const a of document.querySelectorAll('a[href^="mailto:"]')) {
        for (const email of (a.getAttribute('href') || '').slice(7).split('?')[0].match(emailPattern) || []) emails.add(email);
      }
      const resources = [];
      for (const el of document.querySelectorAll('img,source,script[src],iframe[src],video[src],video[poster],audio[src],link[href]')) {
        const item = attrs(el);
        const rel = (item.rel || '').toLowerCase();
        if (item.tag === 'link' && !/(stylesheet|icon|preload|manifest)/.test(rel)) continue;
        const raw = item.src || item.href || el.getAttribute('poster') || '';
        if (raw) resources.push({kind: item.tag + (item.tag === 'link' ? ':' + rel : ''),
          declared_url: new URL(raw, document.baseURI).href, current_url: item.currentSrc || '', alt: item.alt,
          loading: item.loading});
      }
      const images = [...document.images].map((image, index) => ({index, url: image.currentSrc || image.src || '',
        declared_url: image.getAttribute('src') || '', alt: image.alt || '', loading: image.loading || 'eager',
        complete: image.complete, natural_width: image.naturalWidth,
        failed: image.complete && image.naturalWidth === 0}));
      const text = document.body?.innerText || '';
      const copyright_years = [...text.matchAll(/(?:©|copyright)\\s*(?:\\d{4}\\s*[-–]\\s*)?(20\\d{2})/ig)].map(m => Number(m[1]));
      const dates = [];
      for (const t of document.querySelectorAll('time[datetime]')) {
        const article = t.closest('article');
        dates.push({date: t.getAttribute('datetime'), text: (t.innerText || '').trim().slice(0, 100),
          source_url: article?.querySelector('a[href]')?.href || location.href, source: 'visible_time'});
      }
      for (const m of document.querySelectorAll('meta[property],meta[name]')) {
        const key = (m.getAttribute('property') || m.getAttribute('name') || '').toLowerCase();
        if (/datepublished|datemodified|article:published_time|article:modified_time|last-modified/.test(key))
          dates.push({date: m.content || '', text: '', source_url: location.href, source: 'meta:' + key});
      }
      for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
        try {
          const walk = (obj, inherited) => {
            if (Array.isArray(obj)) { for (const x of obj) walk(x, inherited); return; }
            if (!obj || typeof obj !== 'object') return;
            const source = obj.url || obj['@id'] || obj.mainEntityOfPage?.['@id'] || inherited || location.href;
            for (const [key, value] of Object.entries(obj)) {
              if (/^(datePublished|dateModified|dateCreated|lastReviewed)$/i.test(key) && typeof value === 'string')
                dates.push({date: value, text: '', source_url: new URL(source, location.href).href, source: 'jsonld:' + key});
              else if (value && typeof value === 'object') walk(value, source);
            }
          };
          walk(JSON.parse(s.textContent || 'null'), location.href);
        } catch (_) {}
      }
      const privacy_links = anchors.filter(a => /privacy/i.test(a.href + ' ' + a.text)).map(a => a.href);
      const contact_links = anchors.filter(a => /contact|contact-us|get-in-touch|reach-us/i.test(a.href + ' ' + a.text)).map(a => a.href);
      const blog_links = anchors.filter(a => /blog|news|journal|articles|stories|updates/i.test(a.href + ' ' + a.text)).map(a => a.href);
      const project_links = anchors.filter(a => /portfolio|projects|case-stud|gallery|our-work|recent-work/i.test(a.href + ' ' + a.text)).map(a => a.href);
      const twitter_assets = [];
      for (const el of document.querySelectorAll('img,svg,[class*="twitter" i],[id*="twitter" i]')) {
        const evidence = [el.getAttribute('src'), el.getAttribute('href'), el.getAttribute('aria-label'),
          el.getAttribute('title'), el.getAttribute('class'), el.getAttribute('id'), el.getAttribute('alt')]
          .filter(Boolean).join(' ').slice(0, 500);
        if (/twitter(?:[-_ ]?(?:bird|logo|icon))?|icon-twitter/i.test(evidence) && visible(el))
          twitter_assets.push({tag: el.tagName.toLowerCase(), evidence});
      }
      const x_assets = [];
      for (const el of document.querySelectorAll('img,svg,[class*="icon-x" i],[class*="logo-x" i],[class*="x-social" i]')) {
        const evidence = [el.getAttribute('src'), el.getAttribute('href'), el.getAttribute('aria-label'),
          el.getAttribute('title'), el.getAttribute('class'), el.getAttribute('id'), el.getAttribute('alt')]
          .filter(Boolean).join(' ').slice(0, 500);
        const xLabel = /^(?:x|x social)$/i.test((el.getAttribute('aria-label') || '').trim());
        if ((/(?:^|[\\s/_-])(?:x-logo|logo-x|icon-x|x-social)(?:$|[\\s/_-])/i.test(evidence) || xLabel) && visible(el))
          x_assets.push({tag: el.tagName.toLowerCase(), evidence});
      }
      const controls = [...document.querySelectorAll('button,[role="button"],[role="tab"]')].map((el, index) => ({
        index, tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '',
        text: (el.innerText || el.getAttribute('aria-label') || el.title || '').trim().slice(0, 120),
        expanded: el.getAttribute('aria-expanded'), selected: el.getAttribute('aria-selected'),
        controls: el.getAttribute('aria-controls'), type: (el.getAttribute('type') || '').toLowerCase(),
        in_form: !!el.closest('form'), visible: visible(el),
        safe_candidate: visible(el) && !el.closest('form') && el.type !== 'submit' &&
          (el.hasAttribute('aria-expanded') || el.hasAttribute('aria-controls') || el.getAttribute('role') === 'tab')
      }));
      const anchors_on_page = new Set([...document.querySelectorAll('[id]')].map(el => el.id));
      for (const el of document.querySelectorAll('a[name]')) anchors_on_page.add(el.name);
      const broken_anchors = anchors.filter(a => a.href.includes('#') && new URL(a.href).origin === location.origin &&
        new URL(a.href).pathname === location.pathname && new URL(a.href).hash.length > 1 &&
        !anchors_on_page.has(decodeURIComponent(new URL(a.href).hash.slice(1))))
        .map(a => ({url: a.href, text: a.text}));
      const formDetails = [...document.forms].slice(0, 20).map(f => ({
          action: new URL(f.getAttribute('action') || location.href, location.href).href, method: (f.method || 'get').toUpperCase(),
          fields: f.querySelectorAll('input,textarea,select').length, detection_method: 'html_form'}));
      const inferredGroups = new Set();
      for (const submitter of document.querySelectorAll('button,input[type="submit"],input[type="image"]')) {
        const isSubmitter = submitter.matches('input[type="submit"],input[type="image"]') ||
          (submitter.tagName.toLowerCase() === 'button' && submitter.type === 'submit');
        if (!isSubmitter) continue;
        if (!visible(submitter) || submitter.closest('form')) continue;
        let parent = submitter.parentElement;
        for (let depth = 0; parent && depth < 7; depth++, parent = parent.parentElement) {
          const fields = [...parent.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]),textarea,select')]
            .filter(visible);
          const evidence = `${parent.id || ''} ${String(parent.className || '')}`;
          if (fields.length >= 2 && /form|contact|email|message|quote|inquir|request/i.test(evidence)) {
            inferredGroups.add(parent);
            break;
          }
        }
      }
      const inferredForms = [...inferredGroups].slice(0, 20).map(group => ({
        action: location.href, method: null,
        fields: group.querySelectorAll('input:not([type="hidden"]):not([type="submit"]):not([type="button"]),textarea,select').length,
        detection_method: 'visible_form_like_controls'}));
      const forms = [...formDetails, ...inferredForms].slice(0, 20);
      return {url: location.href, form_count: document.forms.length + inferredForms.length, forms,
        emails: [...emails].slice(0, 30),
        title: document.title, viewport_meta: !!document.querySelector('meta[name="viewport"]'),
        document_width: document.documentElement.scrollWidth, viewport_width: document.documentElement.clientWidth,
        image_count: images.length, loaded_image_count: images.filter(i => i.complete && i.natural_width > 0).length,
        failed_images: images.filter(i => i.failed).slice(0, 60), pending_images: images.filter(i => !i.complete).slice(0, 30),
        images: images.slice(0, 300), anchors, resources: resources.slice(0, 500), dates: dates.slice(0, 100),
        privacy_links: [...new Set(privacy_links)].slice(0, 30), contact_links: [...new Set(contact_links)].slice(0, 30),
        blog_links: [...new Set(blog_links)].slice(0, 30), project_links: [...new Set(project_links)].slice(0, 30),
        copyright_years,
        has_contact_path: contact_links.length > 0, has_cta: anchors.some(a => /contact|call|visit|book|schedule|request|get a quote|estimate|get in touch/i.test(a.text)),
        twitter_assets: twitter_assets.slice(0, 20), x_assets: x_assets.slice(0, 20),
        legacy_twitter_branding: twitter_assets.length > 0 && x_assets.length === 0,
        safe_controls: controls.filter(c => c.safe_candidate).slice(0, 30), broken_anchors: broken_anchors.slice(0, 60),
        copyright_years};
    }""")


async def _safe_http_response(context, url: str, method: str, public_cache: dict[str, bool],
                              public_host_fn, same_site_base: str | None = None):
    """Follow a short public redirect chain manually so private redirect targets are never fetched."""
    current = url
    chain = []
    for _ in range(5):
        host = _host(current)
        if host not in public_cache:
            public_cache[host] = await asyncio.to_thread(public_host_fn, host)
        if not public_cache[host]:
            return None, current, chain, "blocked_non_public_redirect", method
        if same_site_base and not _same_site(current, same_site_base):
            return None, current, chain, "redirected_off_site", method
        try:
            if method == "HEAD":
                response = await context.request.head(current, timeout=REQUEST_TIMEOUT_MS, max_redirects=0)
                if response.status in {405, 501}:
                    method = "GET_range"
                    response = await context.request.get(current, timeout=REQUEST_TIMEOUT_MS, max_redirects=0,
                                                         headers={"Range": "bytes=0-0"})
            else:
                response = await context.request.get(current, timeout=REQUEST_TIMEOUT_MS, max_redirects=0,
                                                     headers={"Range": "bytes=0-262143"})
        except Exception as exc:
            return None, current, chain, f"{type(exc).__name__}: {exc}"[:250], method
        location = response.headers.get("location")
        if response.status in {301, 302, 303, 307, 308} and location:
            destination = urljoin(current, location)
            if urlsplit(destination).scheme.lower() not in {"http", "https"}:
                return response, current, chain, "redirected_to_unsupported_scheme", method
            chain.append({"url": current, "status": response.status, "location": destination})
            if same_site_base and not _same_site(destination, same_site_base):
                return response, destination, chain, "redirected_off_site", method
            current = destination
            continue
        return response, response.url, chain, None, method
    return None, current, chain, "too_many_redirects", method


async def _request_page(context, url: str, public_cache: dict[str, bool], public_host_fn,
                        same_site_base: str) -> tuple[dict | None, str | None]:
    """Fetch a same-site contact/blog page with safe redirects and a strict timeout."""
    try:
        response, final_url, redirect_chain, blocked_reason, _method = await _safe_http_response(
            context, url, "GET_range", public_cache, public_host_fn, same_site_base)
        if blocked_reason or response is None:
            return {"url": url, "final_url": final_url, "redirect_chain": redirect_chain,
                    "status": response.status if response else None, "error": blocked_reason}, None
        body = (await response.text())[:262144]
        parsed = PageEvidenceParser(final_url)
        parsed.feed(body)
        return {"url": url, "final_url": final_url, "status": response.status,
                "redirected": bool(redirect_chain), "redirect_chain": redirect_chain, "form_count": parsed.forms,
        "emails": sorted(set(parsed.emails))[:30],
        "privacy_links": sorted(set(parsed.privacy_links)), "blog_links": sorted(set(parsed.blog_links)),
        "project_links": sorted(set(parsed.project_links)),
                "contact_links": sorted(set(parsed.contact_links)), "date_candidates": parsed.date_candidates,
                "links": parsed.links[:300], "last_modified": response.headers.get("last-modified")}, None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"[:250]


async def _probe_sitemap_date(context, base_url: str, public_cache: dict[str, bool], public_host_fn) -> dict | None:
    """Read at most robots.txt and two same-site sitemap documents as a weak freshness clue."""
    parts = urlsplit(base_url)
    origin = f"{parts.scheme}://{parts.netloc}/"
    robots_url = urljoin(origin, "robots.txt")
    sitemap_urls = []
    try:
        response, _final_url, _chain, error, _method = await _safe_http_response(
            context, robots_url, "GET_range", public_cache, public_host_fn, base_url)
        if response and not error and response.status < 400:
            body = (await response.text())[:65536]
            sitemap_urls.extend(urljoin(robots_url, match) for match in
                                re.findall(r"(?im)^\s*sitemap:\s*(\S+)", body))
    except Exception:
        pass
    if not sitemap_urls:
        sitemap_urls = [urljoin(origin, "sitemap.xml")]
    first = next((url for url in sitemap_urls if _same_site(url, base_url)), None)
    if not first:
        return None
    docs = [first]
    latest = None
    index = 0
    while index < len(docs) and index < 2:
        url = docs[index]
        index += 1
        host = _host(url)
        if host not in public_cache:
            public_cache[host] = await asyncio.to_thread(public_host_fn, host)
        if not public_cache[host]:
            continue
        try:
            response, final_url, _chain, error, _method = await _safe_http_response(
                context, url, "GET_range", public_cache, public_host_fn, base_url)
            if error or response is None or response.status >= 400:
                continue
            xml = (await response.text())[:262144]
            entries = re.findall(r"(?is)<(?:url|sitemap)\b[^>]*>(.*?)</(?:url|sitemap)>", xml)
            for entry in entries:
                loc_match = re.search(r"(?is)<loc\b[^>]*>(.*?)</loc>", entry)
                last_match = re.search(r"(?is)<lastmod\b[^>]*>(.*?)</lastmod>", entry)
                loc = urljoin(final_url, html.unescape(loc_match.group(1).strip())) if loc_match else final_url
                lastmod = html.unescape(last_match.group(1).strip()) if last_match else ""
                normalized = _iso_date(lastmod)
                if not normalized:
                    try:
                        normalized = parsedate_to_datetime(lastmod).date().isoformat()
                    except (TypeError, ValueError, OverflowError):
                        normalized = None
                if normalized and (latest is None or normalized > latest["date"]):
                    latest = {"date": normalized, "source_url": loc, "source": "sitemap_lastmod_weak"}
                elif loc.lower().endswith(".xml") and _same_site(loc, base_url) and len(docs) < 2:
                    docs.append(loc)
        except Exception:
            continue
    return latest


async def _checked_link(context, item: dict, public_cache: dict[str, bool], public_host_fn) -> dict:
    url = item["url"]
    try:
        response, final_url, redirect_chain, error, method = await _safe_http_response(
            context, url, "HEAD", public_cache, public_host_fn)
        status = response.status if response else None
        if error:
            result = "skipped_non_public_host" if error == "blocked_non_public_redirect" else "request_failed"
            return {**item, "status": status, "method": method, "final_url": final_url,
                    "redirected": bool(redirect_chain), "redirect_chain": redirect_chain,
                    "ends_on_https": bool(final_url and urlsplit(final_url).scheme.lower() == "https"),
                    "result": result, "error": error}
        broken = status in {404, 410} or (status is not None and status >= 500)
        insecure = _is_http(final_url or "")
        restricted = status in {401, 403, 429}
        return {**item, "status": status, "method": method if response else None,
                "final_url": final_url, "redirected": bool(redirect_chain), "redirect_chain": redirect_chain,
                "ends_on_https": bool(final_url and urlsplit(final_url).scheme.lower() == "https"),
                "result": "broken" if broken else "insecure_http" if insecure else
                          "restricted_or_rate_limited" if restricted else "clear"}
    except Exception as exc:
        return {**item, "status": None, "final_url": None, "redirected": None,
                "ends_on_https": None, "result": "request_failed", "error": f"{type(exc).__name__}: {exc}"[:250]}


def _freshest_date(candidates: list[dict]) -> dict | None:
    normalized = []
    for item in candidates:
        value = _iso_date(item.get("date"))
        if value:
            normalized.append({**item, "date": value})
    return max(normalized, key=lambda row: row["date"]) if normalized else None


def _age_fields(value: str) -> dict:
    normalized = _iso_date(value)
    if not normalized:
        return {"age_days": None, "age_band": "unknown"}
    age_days = (date.today() - date.fromisoformat(normalized)).days
    if age_days < 0:
        band = "future_dated"
    elif age_days < 730:
        band = "under_2_years"
    elif age_days < 1826:
        band = "2_to_5_years"
    else:
        band = "over_5_years"
    return {"age_days": age_days, "age_band": band}


def _copyright_footer_signal(years: list[int], current_year: int | None = None) -> dict:
    current_year = current_year or date.today().year
    displayed_years = sorted(set(year for year in years if isinstance(year, int)))
    older_years = [year for year in displayed_years if year <= current_year - 5]
    return {"status": "possible_stale_footer" if older_years else "no_old_footer_year_detected" if displayed_years else "not_found",
            "displayed_years": displayed_years, "older_years": older_years,
            "counts_as_content_freshness": False,
            "note": "Copyright years can be automatically generated and do not prove when page content was updated."}


async def _smoke_test_controls(page, controls: list[dict]) -> dict:
    results = []
    locator = page.locator('button,[role="button"],[role="tab"]')
    for item in controls[:MAX_INTERACTIONS]:
        index = item.get("index")
        text = item.get("text", "")
        if item.get("type") == "submit" or item.get("in_form"):
            continue
        if re.search(r"submit|send|purchase|checkout|buy|donate|delete|remove|sign in|log in|account", text, re.I):
            continue
        target = locator.nth(index)
        try:
            if not await target.is_visible():
                continue
            before = await target.evaluate("""el => {
              const controlled = (el.getAttribute('aria-controls') || '').split(/\\s+/).filter(Boolean)
                .map(id => document.getElementById(id)).filter(Boolean);
              return JSON.stringify({expanded: el.getAttribute('aria-expanded'), selected: el.getAttribute('aria-selected'),
                className: String(el.className || '').slice(0, 200),
                controlled: controlled.map(x => ({hidden: x.hidden, ariaHidden: x.getAttribute('aria-hidden'),
                  text: (x.innerText || '').slice(0, 300)}))});
            }""")
            await target.click(timeout=2200)
            await page.wait_for_timeout(150)
            after = await target.evaluate("""el => {
              const controlled = (el.getAttribute('aria-controls') || '').split(/\\s+/).filter(Boolean)
                .map(id => document.getElementById(id)).filter(Boolean);
              return JSON.stringify({expanded: el.getAttribute('aria-expanded'), selected: el.getAttribute('aria-selected'),
                className: String(el.className || '').slice(0, 200),
                controlled: controlled.map(x => ({hidden: x.hidden, ariaHidden: x.getAttribute('aria-hidden'),
                  text: (x.innerText || '').slice(0, 300)}))});
            }""")
            url_changed = bool(page.url != item.get("page_url")) if item.get("page_url") else False
            changed = before != after or url_changed
            results.append({"text": text, "role": item.get("role") or item.get("tag"),
                            "result": "responded" if changed else "no_observable_change",
                            "state_changed": changed, "before": before[:500], "after": after[:500],
                            "url_changed": url_changed})
            if item.get("expanded") in {"false", None}:
                try:
                    after_obj = json.loads(after)
                    if after_obj.get("expanded") == "true":
                        await target.click(timeout=1800)
                except Exception:
                    pass
        except Exception as exc:
            results.append({"text": text, "role": item.get("role") or item.get("tag"),
                            "result": "interaction_error", "error": f"{type(exc).__name__}: {exc}"[:250]})
    failed = [row for row in results if row["result"] != "responded"]
    return {"status": "issues_found" if failed else "clear" if results else "not_applicable",
            "tested_count": len(results), "findings": results,
            "scope_note": "Smoke test only for accessible menu/disclosure/tab controls; form submissions and purchase/account actions are excluded."}


async def run_site_checks(page, base_url: str, observer: PageObserver, public_host_cache: dict[str, bool],
                          public_host_fn, image_loading: dict | None = None) -> dict:
    """Return bounded script findings for the already-loaded homepage."""
    try:
        facts = await collect_dom_facts(page)
    except Exception as exc:
        return {"status": "unknown", "error": f"Could not inspect page DOM: {type(exc).__name__}: {exc}"[:300]}
    facts["image_loading"] = image_loading or {"complete": False, "note": "Lazy-load pass was not recorded."}

    # Page-local fragment links can be verified without making network requests.
    anchors = facts.get("anchors", [])
    fragment_issues = facts.get("broken_anchors", [])
    selected, seen = [], set()
    http_anchor_records = []
    for link in anchors:
        url = link.get("href", "")
        if not url.startswith(("http://", "https://")):
            continue
        internal = _same_site(url, base_url)
        if _is_http(url):
            http_anchor_records.append({"url": urlsplit(url)._replace(fragment="").geturl(),
                                        "text": link.get("text", "")[:160], "kind": "http_link",
                                        "internal": internal})
        if not (_is_http(url) or internal):
            continue
        key = (urlsplit(url)._replace(fragment="").geturl(), "http_link" if _is_http(url) else "internal_link")
        if key in seen:
            continue
        seen.add(key)
        selected.append({"url": key[0], "text": link.get("text", "")[:160],
                         "kind": key[1], "internal": internal})
    selected.sort(key=lambda item: (item["kind"] != "http_link", not item["internal"]))
    link_semaphore = asyncio.Semaphore(4)
    async def bounded_check(item):
        async with link_semaphore:
            return await _checked_link(page.context, item, public_host_cache, public_host_fn)
    checked = await asyncio.gather(*[bounded_check(item) for item in selected[:MAX_LINK_CHECKS]])
    internal_checked = [item for item in checked if item.get("internal")]
    broken_links = [item for item in internal_checked if item.get("result") in {"broken", "request_failed"}]
    checked_http_by_url = {item.get("url"): item for item in checked if item.get("kind") == "http_link"}
    http_links = [{**item, **checked_http_by_url[item["url"]]} if item["url"] in checked_http_by_url else
                  {**item, "status": None, "final_url": None, "redirected": None,
                   "ends_on_https": None, "result": "not_checked_limit"}
                  for item in http_anchor_records]

    resource_events = [item for item in observer.responses if item.get("resource_type") != "document"]
    failed_events = [item for item in observer.failures if item.get("resource_type") in {"image", "stylesheet", "script", "font", "media"}]
    raw_http_resources = [item for item in facts.get("resources", []) if _is_http(item.get("declared_url", "")) or _is_http(item.get("current_url", ""))]
    insecure_resources = []
    for item in raw_http_resources[:100]:
        raw_url = item.get("declared_url") or item.get("current_url")
        observed = next((row for row in resource_events if row.get("url") in {raw_url, item.get("current_url")} or
                         row.get("final_url") in {raw_url, item.get("current_url")}), None)
        failed = next((row for row in failed_events if row.get("url") in {raw_url, item.get("current_url")}), None)
        insecure_resources.append({**item, "status": observed.get("status") if observed else None,
                                   "final_url": observed.get("final_url") if observed else None,
                                   "redirected": observed.get("redirected") if observed else None,
                                   "ends_on_https": (urlsplit(observed["final_url"]).scheme.lower() == "https") if observed else None,
                                   "request_failure": failed.get("failure") if failed else None,
                                   "result": "still_http" if observed and _is_http(observed.get("final_url", "")) else
                                             "request_failed" if failed else "hardcoded_http_not_observed" if not observed else "upgraded_to_https"})
    def resource_seen(url: str) -> bool:
        return any(row.get("declared_url") == url or row.get("final_url") == url for row in insecure_resources)
    for row in resource_events:
        if row.get("resource_type") == "document" or not (_is_http(row.get("url", "")) or _is_http(row.get("final_url", ""))):
            continue
        if resource_seen(row.get("url", "")):
            continue
        insecure_resources.append({"kind": row.get("resource_type"), "declared_url": row.get("url"),
                                   "status": row.get("status"), "final_url": row.get("final_url"),
                                   "redirected": row.get("redirected"),
                                   "ends_on_https": urlsplit(row.get("final_url", "")).scheme.lower() == "https",
                                   "result": "still_http" if _is_http(row.get("final_url", "")) else "upgraded_to_https",
                                   "source": "browser_request"})
    for row in failed_events:
        if row.get("resource_type") not in {"image", "stylesheet", "script", "font", "media"} or not _is_http(row.get("url", "")):
            continue
        if not resource_seen(row.get("url", "")):
            insecure_resources.append({"kind": row.get("resource_type"), "declared_url": row.get("url"),
                                       "status": None, "final_url": None, "redirected": None,
                                       "ends_on_https": None, "request_failure": row.get("failure"),
                                       "result": "request_failed", "source": "browser_request"})
    http_status = "issues_found" if any(row.get("result") in {"broken", "insecure_http", "request_failed"} for row in http_links) or insecure_resources else "clear"

    failed_images = [{**item, "checked_after_lazy_load": bool(image_loading and image_loading.get("complete"))}
                     for item in facts.get("failed_images", [])]
    image_request_failures = [row for row in failed_events if row.get("resource_type") == "image"]
    images_status = "issues_found" if failed_images or image_request_failures else "partial" if facts.get("pending_images") or not facts.get("image_loading", {}).get("complete", True) else "clear"

    # Inspect linked contact pages when the homepage lacks either a form or a public email.
    contact_pages = []
    form_count = facts.get("form_count", 0)
    emails = list(facts.get("emails", []))
    if not form_count or not emails:
        for candidate in facts.get("contact_links", [])[:2]:
            if _same_site(candidate, base_url):
                checked_page, error = await _request_page(page.context, candidate, public_host_cache, public_host_fn, base_url)
                if checked_page:
                    contact_pages.append(checked_page)
                    form_count += checked_page.get("form_count", 0)
                    emails.extend(checked_page.get("emails", []))
                elif error:
                    contact_pages.append({"url": candidate, "error": error})
                if form_count and emails:
                    break
    privacy_links = sorted(set(facts.get("privacy_links", []) + [url for cp in contact_pages for url in cp.get("privacy_links", [])]))
    form_privacy = {"status": "issues_found" if form_count and not privacy_links else "clear" if form_count and privacy_links else "not_applicable",
                    "form_detected": form_count > 0, "form_count": form_count,
                    "forms_detected": facts.get("forms", [])[:20],
                    "privacy_policy_link_found": bool(privacy_links), "privacy_policy_links": privacy_links[:20],
                    "pages_inspected": [base_url] + [cp.get("final_url") or cp.get("url") for cp in contact_pages],
                    "scope_note": "Presence only; this does not assess legal adequacy. Contact-page checks are limited to linked same-site pages."}
    email_opportunity = _classify_contact_emails(emails, base_url)
    email_opportunity["pages_inspected"] = [base_url] + [cp.get("final_url") or cp.get("url") for cp in contact_pages]

    # Discover linked blog/news pages. If none are linked, try a few conventional read-only routes.
    blog_candidates = [url for url in facts.get("blog_links", []) if _same_site(url, base_url)]
    if not blog_candidates:
        origin = urlsplit(base_url)
        blog_candidates = [urljoin(f"{origin.scheme}://{origin.netloc}/", path) for path in ("blog", "news", "articles")]
    blog_pages, date_candidates = [], list(facts.get("dates", []))
    for candidate in list(dict.fromkeys(blog_candidates))[:3]:
        if not _same_site(candidate, base_url):
            continue
        parsed_page, error = await _request_page(page.context, candidate, public_host_cache, public_host_fn, base_url)
        if parsed_page:
            blog_pages.append(parsed_page)
            date_candidates.extend(parsed_page.get("date_candidates", []))
        elif error:
            blog_pages.append({"url": candidate, "error": error})
    blog_date_candidates = []
    if facts.get("blog_links"):
        blog_date_candidates.extend(row for row in facts.get("dates", []) if row.get("source") == "visible_time")
    for blog_page in blog_pages:
        final_path = urlsplit(blog_page.get("final_url") or blog_page.get("url") or "").path
        for row in blog_page.get("date_candidates", []):
            source = row.get("source", "")
            if (source == "article_time" or (source == "time" and BLOG_RE.search(final_path)) or
                    source == "meta:article:published_time" or source == "meta:datepublished" or
                    (source == "jsonld:datePublished" and
                     (BLOG_RE.search(final_path) or row.get("source_url") != blog_page.get("final_url")))):
                blog_date_candidates.append(row)
    latest_post = _freshest_date(blog_date_candidates)
    if latest_post:
        blog_check = {"status": "found", "latest_date": latest_post["date"],
                      "source_url": latest_post.get("source_url") or base_url,
                      "date_source": latest_post.get("source"),
                      **_age_fields(latest_post["date"]),
                      "pages_inspected": [base_url] + [bp.get("final_url") or bp.get("url") for bp in blog_pages]}
    else:
        blog_check = {"status": "not_found", "latest_date": None, "source_url": None,
                      "date_source": None, "age_days": None, "age_band": "unknown",
                      "pages_inspected": [base_url] + [bp.get("final_url") or bp.get("url") for bp in blog_pages],
                      "note": "No published date found in visible time elements, date metadata, structured data, or inspected blog/news routes."}

    # Freshness is evidence, not a claim that a page was definitely edited on a date.
    freshness_candidates = list(date_candidates)
    freshness_candidates.extend(row for cp in contact_pages for row in cp.get("date_candidates", []))
    project_pages = []
    project_candidates = [url for url in facts.get("project_links", []) if _same_site(url, base_url)]
    for candidate in list(dict.fromkeys(project_candidates))[:2]:
        parsed_page, error = await _request_page(page.context, candidate, public_host_cache, public_host_fn, base_url)
        if parsed_page:
            project_pages.append(parsed_page)
            freshness_candidates.extend(parsed_page.get("date_candidates", []))
        elif error:
            project_pages.append({"url": candidate, "error": error})
    for item in observer.responses:
        if item.get("resource_type") == "document" and _same_site(item.get("final_url", ""), base_url):
            # Last-Modified is captured separately from API responses below; response bodies do not prove edit time.
            pass
    weak_dates = []
    for cp in contact_pages + blog_pages + project_pages:
        if cp.get("last_modified"):
            weak_dates.append({"date": cp["last_modified"], "source_url": cp.get("final_url") or cp.get("url"),
                               "source": "http_last_modified_weak"})
    for item in observer.responses:
        if item.get("resource_type") == "document" and item.get("last_modified"):
            weak_dates.append({"date": item["last_modified"], "source_url": item.get("final_url") or base_url,
                               "source": "http_last_modified_weak"})
    sitemap_date = await _probe_sitemap_date(page.context, base_url, public_host_cache, public_host_fn)
    if sitemap_date:
        weak_dates.append(sitemap_date)
    trusted_freshness = _freshest_date(freshness_candidates)
    weak_freshness = _freshest_date(weak_dates)
    if trusted_freshness:
        freshness = {"status": "dated_content_found", "latest_date": trusted_freshness["date"],
                     "latest_observed_content_date": trusted_freshness["date"],
                     "source_url": trusted_freshness.get("source_url"), "source": trusted_freshness.get("source"),
                     **_age_fields(trusted_freshness["date"]),
                     "confidence": "medium" if trusted_freshness.get("source", "").startswith("jsonld:") or trusted_freshness.get("source") in {"visible_time", "article_time"} else "low",
                     "pages_inspected": [base_url] + [cp.get("final_url") or cp.get("url") for cp in contact_pages + blog_pages + project_pages]}
    elif weak_freshness:
        freshness = {"status": "weak_date_clue_only", "latest_date": weak_freshness["date"],
                     "latest_observed_content_date": None,
                     "source_url": weak_freshness.get("source_url"), "source": weak_freshness.get("source"),
                     **_age_fields(weak_freshness["date"]),
                     "confidence": "low", "note": "Sitemap and HTTP Last-Modified values may not reflect a meaningful content update. Copyright years are excluded from content freshness.",
                     "pages_inspected": [base_url] + [cp.get("final_url") or cp.get("url") for cp in contact_pages + blog_pages + project_pages]}
    else:
        freshness = {"status": "unknown", "latest_date": None, "latest_observed_content_date": None,
                     "age_days": None, "age_band": "unknown", "source_url": None, "source": None,
                     "confidence": "none", "note": "No trustworthy content date was found; absence of dates is not proof that the site is stale.",
                     "pages_inspected": [base_url] + [cp.get("final_url") or cp.get("url") for cp in contact_pages + blog_pages + project_pages]}

    interactions = await _smoke_test_controls(page, [dict(item, page_url=base_url) for item in facts.get("safe_controls", [])])
    console_errors = observer.console_errors[-50:]
    network_http_resources = [row for row in insecure_resources if row.get("source") == "browser_request"]
    twitter = {"status": "possible_legacy_twitter_logo" if facts.get("legacy_twitter_branding") else "not_detected",
               "legacy_twitter_logo_detected": bool(facts.get("legacy_twitter_branding")),
               "twitter_assets": facts.get("twitter_assets", []), "x_assets": facts.get("x_assets", []),
               "twitter_account_links": [a.get("href") for a in anchors if "twitter.com" in a.get("href", "")][:20],
               "note": "A Twitter account link alone is not treated as evidence of an old logo; asset labels and identifiers are heuristic and should be visually confirmed."}
    copyright_signal = _copyright_footer_signal(facts.get("copyright_years", []))

    result = {
        "status": "partial" if len(selected) > MAX_LINK_CHECKS or facts.get("pending_images") else "completed",
        "limits": {"max_link_checks": MAX_LINK_CHECKS, "max_lazy_images": MAX_LAZY_IMAGES,
                   "max_interactions": MAX_INTERACTIONS, "request_timeout_ms": REQUEST_TIMEOUT_MS},
        "http_links_and_resources": {"status": "issues_found" if http_status == "issues_found" else "clear",
            "http_link_count": len(http_anchor_records), "http_links": http_links[:300],
            "insecure_resource_count": len(insecure_resources), "insecure_resources": insecure_resources[:150],
            "insecure_resources_truncated": len(insecure_resources) > 150,
            "network_observed_http_resources": network_http_resources[:100],
            "checked_link_limit_reached": len(selected) > MAX_LINK_CHECKS},
        "broken_images": {"status": images_status, "lazy_loading": facts.get("image_loading", {}),
            "failed_image_count": len(failed_images) + len(image_request_failures),
            "images": failed_images[:60], "request_failures": image_request_failures[:60],
            "pending_images": facts.get("pending_images", [])[:30]},
        "contact_form_privacy": form_privacy,
        "contact_email_opportunity": email_opportunity,
        "broken_links_and_interactions": {"status": "issues_found" if broken_links or fragment_issues or interactions["status"] == "issues_found" or console_errors else "clear",
            "internal_links_checked": len(internal_checked), "broken_internal_links": broken_links[:60],
            "broken_same_page_anchors": fragment_issues[:60], "interactions": interactions,
            "console_errors": console_errors, "request_failures": observer.failures[:100]},
        "latest_blog_post": blog_check,
        "content_freshness": freshness,
        "copyright_footer_signal": copyright_signal,
        "legacy_twitter_branding": twitter,
    }
    return _safe_payload(result)
