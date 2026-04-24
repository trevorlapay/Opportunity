"""
scraper.py — Deterministic scraping engine.

Supports strategy types:
  html_list            — Static HTML with a list of item cards
  html_search_result   — Paginated search result page (static HTML)
  rss_feed             — RSS/Atom feed via feedparser
  json_api             — Public JSON endpoint (GET or POST)
  sitemap              — XML sitemap URL matching
  playwright           — JavaScript-rendered pages (requires playwright package)
  workday_api          — Workday ATS JSON API (POST to /wday/cxs endpoint)

Each source has its strategy and CSS selectors / API config defined in sources.json.
The scraper makes no AI API calls — that is reserved for healer.py.

Playwright is optional: if not installed the strategy falls back to requests+BS4.
"""

import hashlib
import json
import logging
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Playwright availability (optional dependency)
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False
    logger.info("playwright not installed — playwright strategy will fall back to requests.")

# Common browser-like headers to avoid trivial bot blocks
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_REQUEST_TIMEOUT = 20  # seconds per individual request
_MAX_RESULTS_PER_SOURCE = 50  # cap to keep runs fast


class ScrapeResult:
    """Lightweight container for a single discovered item."""

    __slots__ = ("source_id", "category", "title", "url", "location", "date", "snippet")

    def __init__(
        self,
        source_id: str,
        category: str,
        title: str,
        url: str,
        location: str = "",
        date: str = "",
        snippet: str = "",
    ):
        self.source_id = source_id
        self.category = category
        self.title = title.strip()
        self.url = url.strip()
        self.location = location.strip()
        self.date = date.strip()
        self.snippet = snippet.strip()

    @property
    def fingerprint(self) -> str:
        raw = f"{self.source_id}|{self.title}|{self.url}"
        return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()

    def to_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "source_id": self.source_id,
            "category": self.category,
            "title": self.title,
            "url": self.url,
            "location": self.location,
            "date": self.date,
            "snippet": self.snippet,
        }


# ── URL health check ───────────────────────────────────────────────────────────

def check_url_health(url: str) -> tuple[bool, int]:
    """
    Return (is_healthy, status_code).
    Uses HEAD first, falls back to GET on HEAD refusal.
    """
    try:
        resp = requests.head(url, headers=_HEADERS, timeout=_REQUEST_TIMEOUT, allow_redirects=True)
        if resp.status_code == 405:
            resp = requests.get(url, headers=_HEADERS, timeout=_REQUEST_TIMEOUT, allow_redirects=True)
        return resp.status_code < 400, resp.status_code
    except requests.RequestException as exc:
        logger.warning("Health check failed for %s: %s", url, exc)
        return False, 0


# ── Strategy dispatchers ───────────────────────────────────────────────────────

def scrape_source(source: dict) -> list[ScrapeResult]:
    """
    Main entry point — dispatch to the correct strategy.
    Returns a (possibly empty) list of ScrapeResult objects.
    """
    strategy = source.get("strategy", "html_list")
    url = source["active_url"]
    dispatch = {
        "html_list": _scrape_html_list,
        "html_search_result": _scrape_html_search_result,
        "rss_feed": _scrape_rss_feed,
        "json_api": _scrape_json_api,
        "sitemap": _scrape_sitemap,
        "playwright": _scrape_playwright,
        "workday_api": _scrape_workday_api,
    }
    fn = dispatch.get(strategy)
    if fn is None:
        logger.error("Unknown strategy '%s' for source %s", strategy, source["id"])
        return []
    try:
        results = fn(source, url)
        logger.info(
            "Source %-30s → %d raw items (strategy=%s)",
            source["id"],
            len(results),
            strategy,
        )
        return results
    except Exception as exc:
        logger.error("Scrape error on source %s: %s", source["id"], exc, exc_info=True)
        return []


def _fetch_html(url: str) -> BeautifulSoup | None:
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_REQUEST_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        logger.warning("HTML fetch failed for %s: %s", url, exc)
        return None


def _resolve_link(href: str, base_url: str) -> str:
    if not href:
        return base_url
    if href.startswith("http"):
        return href
    return urljoin(base_url, href)


def _check_dead_content(html: str, patterns: list[str]) -> bool:
    """Return True if any dead-content pattern appears in the HTML (case-insensitive)."""
    lower = html.lower()
    return any(p.lower() in lower for p in patterns)


def _extract_items_from_soup(
    soup: BeautifulSoup,
    source: dict,
    base_url: str,
) -> list[ScrapeResult]:
    """
    Generic CSS-selector-based item extraction used by html_list and
    html_search_result strategies.
    """
    sel = source.get("selectors", {})
    container_sel = sel.get("item_container", "")
    title_sel = sel.get("title", "")
    location_sel = sel.get("location", "")
    link_sel = sel.get("link", "a")
    date_sel = sel.get("date", "")

    if not container_sel:
        return []

    containers = soup.select(container_sel)
    results: list[ScrapeResult] = []

    for container in containers[:_MAX_RESULTS_PER_SOURCE]:
        # Title
        title_el = container.select_one(title_sel) if title_sel else None
        title = (title_el.get_text(strip=True) if title_el else container.get_text(strip=True))[:200]
        if not title:
            continue

        # Link
        link_el = container.select_one(link_sel) if link_sel else None
        href = (link_el.get("href", "") if link_el else "") or ""
        url = _resolve_link(href, base_url)

        # Location
        loc_el = container.select_one(location_sel) if location_sel else None
        location = loc_el.get_text(strip=True) if loc_el else ""

        # Date
        date_el = container.select_one(date_sel) if date_sel else None
        date = date_el.get_text(strip=True) if date_el else ""

        results.append(
            ScrapeResult(
                source_id=source["id"],
                category=source["category"],
                title=title,
                url=url,
                location=location,
                date=date,
            )
        )

    return results


# ── Strategy implementations ───────────────────────────────────────────────────

def _scrape_html_list(source: dict, url: str) -> list[ScrapeResult]:
    soup = _fetch_html(url)
    if soup is None:
        return []
    dead_patterns = source.get("dead_content_patterns", [])
    if _check_dead_content(str(soup), dead_patterns):
        logger.info("Dead-content pattern matched for source %s", source["id"])
        return []
    return _extract_items_from_soup(soup, source, url)


def _scrape_html_search_result(source: dict, url: str) -> list[ScrapeResult]:
    # Same as html_list for now; pagination is a v2 enhancement
    return _scrape_html_list(source, url)


def _scrape_rss_feed(source: dict, url: str) -> list[ScrapeResult]:
    try:
        feed = feedparser.parse(url)
    except Exception as exc:
        logger.warning("RSS parse error for %s: %s", source["id"], exc)
        return []

    if feed.bozo and not feed.entries:
        logger.warning("Malformed RSS for %s: %s", source["id"], feed.bozo_exception)
        return []

    results: list[ScrapeResult] = []
    for entry in feed.entries[:_MAX_RESULTS_PER_SOURCE]:
        title = getattr(entry, "title", "").strip()
        link = getattr(entry, "link", "").strip()
        summary = getattr(entry, "summary", "").strip()[:300]
        published = getattr(entry, "published", "").strip()

        # Attempt to extract a location from tags or content
        location = ""
        tags = getattr(entry, "tags", [])
        if tags:
            location = ", ".join(t.get("term", "") for t in tags if t.get("term"))[:100]

        if title and link:
            results.append(
                ScrapeResult(
                    source_id=source["id"],
                    category=source["category"],
                    title=title,
                    url=link,
                    location=location,
                    date=published,
                    snippet=summary,
                )
            )

    return results


def _scrape_json_api(source: dict, url: str) -> list[ScrapeResult]:
    """
    Generic JSON API scraper. Expects a top-level array or an object with
    a 'results'/'items'/'jobs' key containing an array.
    Selector keys used: 'title_key', 'url_key', 'location_key'.
    """
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data: Any = resp.json()
    except Exception as exc:
        logger.warning("JSON API fetch error for %s: %s", source["id"], exc)
        return []

    # Unwrap common envelope patterns
    if isinstance(data, dict):
        for key in ("results", "items", "jobs", "data", "listings"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        return []

    sel = source.get("selectors", {})
    title_key = sel.get("title_key", "title")
    url_key = sel.get("url_key", "url")
    location_key = sel.get("location_key", "location")

    results: list[ScrapeResult] = []
    for item in data[:_MAX_RESULTS_PER_SOURCE]:
        if not isinstance(item, dict):
            continue
        title = str(item.get(title_key, "")).strip()
        link = str(item.get(url_key, "")).strip()
        location = str(item.get(location_key, "")).strip()
        if title:
            results.append(
                ScrapeResult(
                    source_id=source["id"],
                    category=source["category"],
                    title=title,
                    url=link,
                    location=location,
                )
            )
    return results


def _scrape_sitemap(source: dict, url: str) -> list[ScrapeResult]:
    """
    Parses a sitemap XML for <loc> URLs matching a configured pattern.
    Selector key used: 'url_pattern' (substring match).
    """
    import xml.etree.ElementTree as ET
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as exc:
        logger.warning("Sitemap fetch error for %s: %s", source["id"], exc)
        return []

    pattern = source.get("selectors", {}).get("url_pattern", "")
    results: list[ScrapeResult] = []

    # iter() walks every element; strip the XML namespace so we match <loc>
    # regardless of whether it's declared as sitemaps.org/0.9 or similar.
    for elem in root.iter():
        tag = elem.tag.rsplit("}", 1)[-1]
        if tag != "loc":
            continue
        loc_url = (elem.text or "").strip()
        if pattern and pattern.lower() not in loc_url.lower():
            continue
        # Derive a title from the URL path
        path = urlparse(loc_url).path.rstrip("/").split("/")[-1]
        title = path.replace("-", " ").replace("_", " ").title()
        results.append(
            ScrapeResult(
                source_id=source["id"],
                category=source["category"],
                title=title,
                url=loc_url,
            )
        )
        if len(results) >= _MAX_RESULTS_PER_SOURCE:
            break

    return results


# ── Playwright strategy (JS-rendered pages) ────────────────────────────────────

def _scrape_playwright(source: dict, url: str) -> list[ScrapeResult]:
    """
    Renders the page with a headless Chromium browser via Playwright, then
    extracts items using the same CSS-selector logic as html_list.

    Falls back to requests+BS4 if Playwright is not installed.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        logger.warning(
            "playwright not installed — falling back to requests for %s", source["id"]
        )
        return _scrape_html_list(source, url)

    sel = source.get("selectors", {})
    dead_patterns = source.get("dead_content_patterns", [])
    wait_selector = sel.get("wait_for_selector", "body")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=_HEADERS["User-Agent"],
                locale="en-US",
                viewport={"width": 1280, "height": 900},
            )
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=45_000)

            # Wait for the job list to appear
            try:
                page.wait_for_selector(wait_selector, timeout=15_000)
            except PWTimeoutError:
                logger.warning(
                    "Playwright: wait_for_selector '%s' timed out on %s",
                    wait_selector,
                    source["id"],
                )

            html = page.content()
            browser.close()
    except Exception as exc:
        logger.error("Playwright error on source %s: %s", source["id"], exc)
        return []

    if _check_dead_content(html, dead_patterns):
        logger.info("Dead-content pattern matched for source %s (playwright)", source["id"])
        return []

    soup = BeautifulSoup(html, "html.parser")
    return _extract_items_from_soup(soup, source, url)


# ── Workday ATS strategy (Playwright + network interception) ──────────────────
#
# Workday's /wday/cxs/ JSON API requires a session-scoped CSRF token that is
# only provided after the SPA bootstraps in a real browser.  We solve this by
# loading the portal page in Playwright and intercepting the jobs API response
# that Workday's own SPA fetches.  This gives us the same JSON payload our old
# direct-POST approach targeted, but without any manual CSRF handling.
#
# Fallback chain: Playwright intercept → direct POST (for older/permissive
# Workday tenants) → empty list.

_WORKDAY_HEADERS = {
    **_HEADERS,
    "Content-Type": "application/json",
    "Accept": "application/json",
    "X-Calypso-CSRF-Token": "true",
    "X-Requested-With": "XMLHttpRequest",
}


def _parse_workday_postings(data: dict, source: dict, base_domain: str, portal_url: str) -> list[ScrapeResult]:
    """Convert a Workday jobPostings payload dict → list[ScrapeResult]."""
    postings = data.get("jobPostings", [])
    results: list[ScrapeResult] = []
    for posting in postings[:_MAX_RESULTS_PER_SOURCE]:
        title = posting.get("title", "").strip()
        path = posting.get("externalPath", "").strip()
        location = posting.get("locationsText", "").strip()
        posted_on = posting.get("postedOn", "").strip()
        if not title:
            continue
        if path.startswith("http"):
            full_url = path
        elif base_domain and path:
            full_url = base_domain + path
        else:
            full_url = portal_url
        results.append(
            ScrapeResult(
                source_id=source["id"],
                category=source["category"],
                title=title,
                url=full_url,
                location=location,
                date=posted_on,
            )
        )
    return results


def _scrape_workday_api(source: dict, url: str) -> list[ScrapeResult]:
    """
    Scrapes Workday ATS job portals.

    Strategy (tried in order):
      1. Playwright: load the portal page, intercept the /wday/cxs/ jobs API
         response that Workday's own SPA fires.  Browser handles CSRF natively.
      2. Direct POST to the /wday/cxs/ endpoint (works on tenants that don't
         enforce CSRF, or when Playwright is unavailable).

    sources.json api_config keys:
      base_url    — e.g. https://universalparks.wd1.myworkdayjobs.com
      endpoint    — e.g. /wday/cxs/universalparks/Universal_Parks_Resorts/jobs
      search_text — keywords (default: "manager director")
      limit       — max results (default: 50)
    """
    api_cfg = source.get("api_config", {})
    base_domain = api_cfg.get("base_url", "").rstrip("/")
    endpoint = api_cfg.get("endpoint", "")
    api_url = base_domain + endpoint if (base_domain and endpoint) else url
    search_text = api_cfg.get("search_text", "manager director")
    limit = min(api_cfg.get("limit", _MAX_RESULTS_PER_SOURCE), _MAX_RESULTS_PER_SOURCE)

    # Derive the human-facing portal URL (with /en-US/ locale prefix)
    # e.g. https://universalparks.wd1.myworkdayjobs.com/en-US/Universal_Parks_Resorts
    site_name = endpoint.rstrip("/").split("/")[-1] if endpoint else ""
    portal_url = f"{base_domain}/en-US/{site_name}" if (base_domain and site_name) else url

    # ── Attempt 1: Playwright interception ────────────────────────────────────
    if _PLAYWRIGHT_AVAILABLE:
        results = _workday_via_playwright(source, portal_url, base_domain, search_text, limit)
        if results:
            logger.info("Workday (Playwright): %d postings from %s", len(results), source["id"])
            return results
        logger.info("Workday (Playwright): no postings captured for %s — trying direct POST", source["id"])

    # ── Attempt 2: Direct POST (older/permissive tenants) ─────────────────────
    payload = {
        "appliedFacets": {},
        "limit": limit,
        "offset": 0,
        "searchText": search_text,
    }
    locations = api_cfg.get("locations", [])
    if locations:
        payload["appliedFacets"] = {"locations": locations}

    try:
        resp = requests.post(api_url, headers=_WORKDAY_HEADERS, json=payload, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        results = _parse_workday_postings(data, source, base_domain, portal_url)
        if results:
            logger.info("Workday (direct POST): %d postings from %s", len(results), source["id"])
        return results
    except Exception as exc:
        logger.warning("Workday direct POST failed for %s: %s", source["id"], exc)
        return []


def _workday_via_playwright(
    source: dict,
    portal_url: str,
    base_domain: str,
    search_text: str,
    limit: int,
) -> list[ScrapeResult]:
    """
    Load the Workday portal in Playwright and intercept the jobs API response
    that Workday's own SPA fires.  Returns parsed results or [] on failure.
    """
    captured_data: list[dict] = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=_HEADERS["User-Agent"],
                locale="en-US",
                viewport={"width": 1280, "height": 900},
            )
            page = context.new_page()

            def handle_response(response):
                """Capture any Workday jobs API response."""
                if "/wday/cxs/" in response.url and response.status == 200:
                    try:
                        body = response.json()
                        if "jobPostings" in body:
                            captured_data.append(body)
                    except Exception:
                        pass

            page.on("response", handle_response)

            # Navigate to the portal — Workday SPA will fire /wday/cxs/ requests
            page.goto(portal_url, wait_until="networkidle", timeout=45_000)
            page.wait_for_timeout(3_000)

            # If Workday loaded but didn't auto-search, try typing the search
            if not captured_data:
                try:
                    search_box = page.query_selector('input[type="text"], input[placeholder*="search"], input[aria-label*="search"]')
                    if search_box:
                        search_box.fill(search_text)
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(3_000)
                        page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass

            browser.close()
    except Exception as exc:
        logger.warning("Workday Playwright error for %s: %s", source["id"], exc)
        return []

    if not captured_data:
        return []

    # Use the first captured response (usually the initial page load query)
    data = captured_data[0]
    return _parse_workday_postings(data, source, base_domain, portal_url)
