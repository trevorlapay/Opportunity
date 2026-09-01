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

After a list page is parsed, job items are optionally enriched by fetching the
posting page and reading its schema.org/JobPosting JSON-LD block (see
`enrich_with_detail`). That is where company, salary, employment type,
seniority, and the full description come from — a list card carries almost none
of it. Enrichment is opt-in per call so healer validation scrapes stay cheap.
"""

import hashlib
import html as html_lib
import json
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse

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

# Pagination for html_search_result. Most search endpoints return one page of
# ~10 results; without paging we collect a fraction of what the source offers
# and never reach _MAX_RESULTS_PER_SOURCE.
_MAX_PAGES = int(os.getenv("SCRAPER_MAX_PAGES", "5"))

# Detail enrichment budget. Each enriched item costs one extra HTTP request, so
# these bound both runtime and how hard we lean on any single site.
_MAX_DETAIL_FETCHES_PER_SOURCE = int(os.getenv("SCRAPER_MAX_DETAIL_FETCHES", "25"))
_DETAIL_FETCH_DELAY_SEC = float(os.getenv("SCRAPER_DETAIL_DELAY_SEC", "0.4"))
_SNIPPET_MAX_CHARS = 600

# Whole-run ceiling on time spent enriching. The per-source cap alone doesn't
# bound a run: ~90 live sources at 25 fetches each would blow through
# scheduler.RUN_TIMEOUT_SECONDS and get the run killed mid-flight, losing
# everything. With a budget, enrichment degrades to list-level data instead.
_ENRICHMENT_TIME_BUDGET_SEC = float(os.getenv("SCRAPER_ENRICH_BUDGET_SEC", "1200"))
_enrichment_started_at: float | None = None


def begin_run() -> None:
    """Start the per-run enrichment clock. Call once at pipeline start."""
    global _enrichment_started_at
    _enrichment_started_at = time.monotonic()


def _enrichment_time_left() -> float:
    """Seconds of enrichment budget remaining (infinite if no run started)."""
    if _enrichment_started_at is None:
        return float("inf")
    return _ENRICHMENT_TIME_BUDGET_SEC - (time.monotonic() - _enrichment_started_at)

# Tags that end a line of text when flattening HTML. Inline tags are
# deliberately excluded — see _strip_html.
_BLOCK_TAGS = (
    "p", "div", "section", "article", "header", "footer", "aside",
    "ul", "ol", "li", "dl", "dt", "dd", "table", "tr", "td", "th",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
)


# Tracking parameters that change every request and must be stripped before
# fingerprinting. Without this, sites like LinkedIn (refId, trackingId,
# position, pageNum regenerated every scrape) produce a different fingerprint
# for the same posting on every run, defeating deduplication.
#
# Allowlist params we MUST keep (job-id-bearing): jk (Indeed), currentJobId,
# jobid, postingid, gh_jid (Greenhouse), lever-id. Anything not in the
# allowlist gets dropped.
_FINGERPRINT_QUERY_ALLOWLIST: frozenset[str] = frozenset({
    "jk",              # Indeed job key
    "currentjobid",    # ATS current-job pointer
    "jobid",           # generic job id
    "postingid",       # generic posting id
    "gh_jid",          # Greenhouse job id
    "lever-id",        # Lever job id
    "id",              # last-resort generic id (kept; cleaner is to leave it)
})


def canonical_url(url: str) -> str:
    """
    Normalize a URL for fingerprinting.

    Strips:
      - All query parameters except those in _FINGERPRINT_QUERY_ALLOWLIST
      - Fragment (#anchor)
      - Trailing slash on the path
    Lowercases the scheme and netloc. Path case is preserved (some sites are
    path-case-sensitive). Leaves the rest alone.

    Returns the original string unchanged if URL parsing fails — we'd rather
    fingerprint a noisy URL than crash the pipeline on a malformed input.
    """
    if not url:
        return url
    try:
        parts = urlparse(url.strip())
        if not parts.scheme or not parts.netloc:
            return url.strip()
        kept = [
            (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() in _FINGERPRINT_QUERY_ALLOWLIST
        ]
        new_query = urlencode(kept) if kept else ""
        path = parts.path.rstrip("/") or "/"
        return urlunparse((
            parts.scheme.lower(),
            parts.netloc.lower(),
            path,
            "",            # params (rarely used)
            new_query,
            "",            # fragment
        ))
    except Exception:
        return url.strip()


class ScrapeResult:
    """Container for a single discovered item.

    `title`/`url` come from the list card. Everything from `company` onward is
    usually only available on the posting page and is filled in by
    `enrich_with_detail`; each defaults to "" so an un-enriched item is still
    valid and the email template can skip empty fields.
    """

    __slots__ = (
        "source_id", "category", "title", "url", "location", "date", "snippet",
        "company", "salary", "employment_type", "seniority", "industry", "description",
        # Set by evaluator.py after dedup. Must live in __slots__ or the
        # assignment raises AttributeError.
        "evaluation",
    )

    def __init__(
        self,
        source_id: str,
        category: str,
        title: str,
        url: str,
        location: str = "",
        date: str = "",
        snippet: str = "",
        company: str = "",
        salary: str = "",
        employment_type: str = "",
        seniority: str = "",
        industry: str = "",
        description: str = "",
    ):
        self.source_id = source_id
        self.category = category
        self.title = title.strip()
        self.url = url.strip()
        self.location = location.strip()
        self.date = date.strip()
        self.snippet = snippet.strip()
        self.company = company.strip()
        self.salary = salary.strip()
        self.employment_type = employment_type.strip()
        self.seniority = seniority.strip()
        self.industry = industry.strip()
        self.description = description.strip()
        self.evaluation = {}

    @property
    def fingerprint(self) -> str:
        # Use the canonicalised URL (tracking params stripped) so the same
        # posting fingerprints identically across runs even when the source
        # adds per-request refId / trackingId / position / utm_* noise.
        raw = f"{self.source_id}|{self.title}|{canonical_url(self.url)}"
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
            "company": self.company,
            "salary": self.salary,
            "employment_type": self.employment_type,
            "seniority": self.seniority,
            "industry": self.industry,
            "description": self.description,
            "evaluation": self.evaluation,
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

def scrape_source(source: dict, enrich: bool = False) -> list[ScrapeResult]:
    """
    Main entry point — dispatch to the correct strategy.
    Returns a (possibly empty) list of ScrapeResult objects.

    `enrich=True` additionally fetches each job posting page for company,
    salary, seniority, and description. It is off by default so healer
    validation scrapes (which only care whether a source yields anything)
    don't pay for a detail fetch per item.
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
    except Exception as exc:
        logger.error("Scrape error on source %s: %s", source["id"], exc, exc_info=True)
        return []

    if enrich and results and source.get("category") == "jobs":
        try:
            enrich_with_detail(results, source)
        except Exception as exc:
            # Enrichment is additive — a failure here must never cost us the
            # items we already scraped successfully.
            logger.error("Detail enrichment failed for %s: %s", source["id"], exc)

    # Single-employer sources (a company's own careers page, a Workday tenant)
    # never print the employer on the card because it is the whole site. Let
    # sources.json state it once rather than leaving every item anonymous.
    # Applied last so it is a genuine fallback: a name the posting itself
    # states wins over the one we configured.
    default_company = str(source.get("company", "")).strip()
    if default_company:
        for item in results:
            if not item.company:
                item.company = default_company

    return results


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


def _strip_html(raw: str) -> str:
    """Turn an HTML fragment into readable plain text, keeping line structure.

    Must run BEFORE truncation. Feeds commonly open with a logo <img> whose
    src is a long CDN URL; truncating first spends the whole character budget
    on markup and leaves the reader a fragment of a URL instead of the job.

    Block boundaries are preserved as newlines rather than collapsed to
    spaces, because field-extraction patterns (see _LOCATION_IN_BODY_RE) rely
    on them to know where one labelled line ends and the next begins.
    """
    if not raw:
        return ""
    soup = BeautifulSoup(raw, "html.parser")
    # Break on block boundaries only. A blanket separator="\n" also splits on
    # inline tags, which job feeds sprinkle over individual words — that turns
    # a sentence into one word per line.
    for tag in soup.find_all("br"):
        tag.replace_with("\n")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_after("\n")
    text = html_lib.unescape(soup.get_text())
    # Collapse runs of horizontal whitespace (incl. non-breaking spaces) but
    # keep the newlines we just established.
    lines = (re.sub(r"[^\S\n]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line).strip()


def _clip(text: str, limit: int = _SNIPPET_MAX_CHARS) -> str:
    """Flatten to one line and truncate on a word boundary, for display."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:-")
    return cut + "…"


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
    company_sel = sel.get("company", "")
    snippet_sel = sel.get("snippet", "")

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

        # Company — present on most job cards (LinkedIn puts it in
        # .base-search-card__subtitle) and the single most useful field after
        # the title. Cheap to read here; the detail fetch can still override it.
        company_el = container.select_one(company_sel) if company_sel else None
        company = company_el.get_text(strip=True) if company_el else ""

        # Optional card-level teaser, if the source exposes one.
        snippet_el = container.select_one(snippet_sel) if snippet_sel else None
        snippet = _clip(_strip_html(snippet_el.decode_contents())) if snippet_el else ""

        results.append(
            ScrapeResult(
                source_id=source["id"],
                category=source["category"],
                title=title,
                url=url,
                location=location,
                date=date,
                snippet=snippet,
                company=company,
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


_PAGINATION_PARAMS = ("start", "offset", "page", "from", "pageNum")


def _with_query_param(url: str, key: str, value: int) -> str:
    """Return `url` with `key` set to `value`, preserving everything else."""
    parts = urlparse(url)
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
              if k.lower() != key.lower()]
    params.append((key, str(value)))
    return urlunparse((
        parts.scheme, parts.netloc, parts.path, parts.params,
        urlencode(params), parts.fragment,
    ))


def _detect_pagination(source: dict, url: str) -> tuple[str, int, int] | None:
    """Work out how to page this source: (param, start_value, step).

    Explicit config wins:
        "pagination": {"param": "start", "step": 10, "start": 0}
    Otherwise infer from a paging param already present in active_url — search
    URLs almost always carry one (LinkedIn's guest endpoint ships `start=0`).
    Returns None when there is nothing to page on.
    """
    cfg = source.get("pagination") or {}
    if cfg.get("param"):
        return cfg["param"], int(cfg.get("start", 0)), int(cfg.get("step", 10))

    existing = dict(parse_qsl(urlparse(url).query, keep_blank_values=True))
    for key in existing:
        if key.lower() in (p.lower() for p in _PAGINATION_PARAMS):
            try:
                start = int(existing[key] or 0)
            except ValueError:
                start = 0
            # `page`/`pageNum` count pages; the others count items.
            step = 1 if key.lower() in ("page", "pagenum") else 0
            return key, start, step
    return None


def _scrape_html_search_result(source: dict, url: str) -> list[ScrapeResult]:
    """Paginated search results.

    Search endpoints typically return ~10 items per page. Reading only the
    first page collects a fraction of what the source offers and never gets
    near _MAX_RESULTS_PER_SOURCE, so walk pages until one adds nothing new.
    """
    first_page = _scrape_html_list(source, url)
    plan = _detect_pagination(source, url)
    if not first_page or plan is None or _MAX_PAGES <= 1:
        return first_page

    param, start, step = plan
    # An item-offset source with no configured step: infer it from page one,
    # which is exactly the source's own page size.
    if step == 0:
        step = len(first_page) or 10

    collected = list(first_page)
    seen = {r.fingerprint for r in collected}

    for page_no in range(1, _MAX_PAGES):
        if len(collected) >= _MAX_RESULTS_PER_SOURCE:
            break
        page_url = _with_query_param(url, param, start + page_no * step)
        try:
            page_items = _scrape_html_list(source, page_url)
        except Exception as exc:
            logger.warning("Pagination stopped for %s at page %d: %s",
                           source["id"], page_no, exc)
            break

        fresh = [r for r in page_items if r.fingerprint not in seen]
        if not fresh:
            # Empty page, or the site is echoing page one — either way, done.
            break
        seen.update(r.fingerprint for r in fresh)
        collected.extend(fresh)

    if len(collected) > len(first_page):
        logger.info("Source %-30s → paginated %d → %d items",
                    source["id"], len(first_page), len(collected))
    return collected[:_MAX_RESULTS_PER_SOURCE]


# Job feeds routinely state the location in the body rather than a field. The
# value runs to the end of its line — _strip_html keeps block breaks as
# newlines precisely so this stops before the next labelled field.
_LOCATION_IN_BODY_RE = re.compile(
    r"^\s*(?:headquarters|location|based in|office)\s*[:\-]\s*(.{2,80}?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# "Acme Corp: Senior Director of Operations" — company prefix on the title.
_COMPANY_PREFIX_RE = re.compile(r"^\s*([^:]{2,60}?)\s*:\s+(.{3,})$")


def _rss_location_and_company(title: str, body: str) -> tuple[str, str]:
    """Pull (location, company) out of an RSS entry.

    RSS gives us no structured place or employer, but job feeds put both in
    predictable spots: the location in a "Headquarters: …" line in the body,
    and the company as a "Company: Role" prefix on the title.

    The title is deliberately returned unmodified even when a company prefix
    is found — it feeds the fingerprint, and rewriting it would make every
    already-seen posting look new and blast one enormous duplicate digest.
    The template suppresses the company line when the title already opens
    with it.
    """
    location = ""
    match = _LOCATION_IN_BODY_RE.search(body)
    if match:
        location = match.group(1).strip(" .,;")

    company = ""
    prefix = _COMPANY_PREFIX_RE.match(title)
    if prefix:
        company = prefix.group(1).strip()

    return location, company


def _scrape_rss_feed(source: dict, url: str) -> list[ScrapeResult]:
    # Fetch ourselves rather than letting feedparser do it: feedparser sends no
    # browser User-Agent (some feeds reject that) and turns an HTTP error page
    # into an opaque "malformed feed" instead of a status we can act on.
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_REQUEST_TIMEOUT, allow_redirects=True)
        if resp.status_code >= 400:
            logger.warning("RSS fetch for %s returned HTTP %s — feed may be retired.",
                           source["id"], resp.status_code)
            return []
        feed = feedparser.parse(resp.content)
    except requests.RequestException as exc:
        logger.warning("RSS fetch error for %s: %s", source["id"], exc)
        return []
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
        published = getattr(entry, "published", "").strip()

        # Strip markup BEFORE clipping — see _strip_html.
        body = _strip_html(getattr(entry, "summary", ""))
        location, company = _rss_location_and_company(title, body)

        # NOTE: entry.tags are feed *categories* ("Management and Finance"),
        # not places. Writing them into `location` showed users a category as
        # a location and fed the geography filter nonsense, so they stay out.

        if title and link:
            results.append(
                ScrapeResult(
                    source_id=source["id"],
                    category=source["category"],
                    title=title,
                    url=link,
                    location=location,
                    date=published,
                    snippet=_clip(body),
                    company=company,
                    description=body,
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
    company_key = sel.get("company_key", "company")
    date_key = sel.get("date_key", "date")
    description_key = sel.get("description_key", "description")

    def field(item: dict, key: str) -> str:
        value = item.get(key, "")
        if isinstance(value, dict):  # e.g. {"name": "Acme"} for company
            value = value.get("name") or value.get("label") or ""
        return str(value or "").strip()

    results: list[ScrapeResult] = []
    for item in data[:_MAX_RESULTS_PER_SOURCE]:
        if not isinstance(item, dict):
            continue
        title = field(item, title_key)
        if not title:
            continue
        description = _strip_html(field(item, description_key))
        results.append(
            ScrapeResult(
                source_id=source["id"],
                category=source["category"],
                title=title,
                url=field(item, url_key),
                location=field(item, location_key),
                date=field(item, date_key),
                snippet=_clip(description),
                company=field(item, company_key),
                description=description,
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


# ── Job detail enrichment (schema.org JobPosting) ─────────────────────────────
#
# A list card carries a title, a link, and if we're lucky a location. Everything
# a reader needs to actually triage a role — employer, pay, seniority, what the
# job is — lives on the posting page. Nearly every major board and ATS
# (LinkedIn, Greenhouse, Lever, Indeed, most Workday tenants) publishes that as
# a schema.org/JobPosting JSON-LD block, so one generic parser covers the bulk
# of sources without any per-source selector configuration.

_EMPLOYMENT_TYPE_LABELS = {
    "FULL_TIME": "Full-time",
    "PART_TIME": "Part-time",
    "CONTRACTOR": "Contract",
    "TEMPORARY": "Temporary",
    "INTERN": "Internship",
    "VOLUNTEER": "Volunteer",
    "PER_DIEM": "Per diem",
    "OTHER": "Other",
}

_SALARY_PERIOD_LABELS = {
    "YEAR": "yr", "MONTH": "mo", "WEEK": "wk", "DAY": "day", "HOUR": "hr",
}


def _as_text(value: Any) -> str:
    """Flatten a JSON-LD value (string / number / list / nested object) to text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = [_as_text(v) for v in value]
        return ", ".join(p for p in parts if p)
    if isinstance(value, dict):
        for key in ("name", "value", "credentialCategory", "label", "title"):
            if key in value:
                return _as_text(value[key])
    return ""


def _iter_json_ld(soup: BeautifulSoup):
    """Yield every JSON-LD object on the page, flattening @graph containers."""
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue  # a malformed block on one page must not abort the rest
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if "@graph" in node:
                    stack.append(node["@graph"])
                yield node


def _find_job_posting(soup: BeautifulSoup) -> dict | None:
    for node in _iter_json_ld(soup):
        node_type = node.get("@type", "")
        types = node_type if isinstance(node_type, list) else [node_type]
        if any(str(t).lower() == "jobposting" for t in types):
            return node
    return None


def _format_jsonld_location(job: dict) -> str:
    if str(job.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        return "Remote"

    locations = job.get("jobLocation")
    if isinstance(locations, dict):
        locations = [locations]
    if not isinstance(locations, list):
        return ""

    rendered: list[str] = []
    for place in locations:
        if not isinstance(place, dict):
            continue
        address = place.get("address")
        if isinstance(address, str):
            rendered.append(address.strip())
            continue
        if not isinstance(address, dict):
            continue
        parts = [
            address.get("addressLocality"),
            address.get("addressRegion"),
            # Country only when it adds information beyond a US state.
            address.get("addressCountry") if not address.get("addressRegion") else None,
        ]
        line = ", ".join(_as_text(p) for p in parts if _as_text(p))
        if line:
            rendered.append(line)
    # Preserve order while dropping repeats (multi-site postings repeat a city).
    return "; ".join(dict.fromkeys(rendered))[:150]


def _format_jsonld_salary(job: dict) -> str:
    base = job.get("baseSalary")
    if not isinstance(base, dict):
        return ""
    currency = _as_text(base.get("currency")) or _as_text(base.get("salaryCurrency"))
    value = base.get("value")

    low = high = unit = ""
    if isinstance(value, dict):
        low = _as_text(value.get("minValue"))
        high = _as_text(value.get("maxValue"))
        unit = _as_text(value.get("unitText"))
        if not low and not high:
            low = _as_text(value.get("value"))
    else:
        low = _as_text(value)

    def money(amount: str) -> str:
        try:
            return f"{float(amount):,.0f}"
        except (TypeError, ValueError):
            return amount

    if low and high and low != high:
        amount = f"{money(low)}–{money(high)}"
    elif low or high:
        amount = money(low or high)
    else:
        return ""

    period = _SALARY_PERIOD_LABELS.get(unit.upper(), unit.lower())
    return " ".join(p for p in (currency, amount) if p) + (f" / {period}" if period else "")


def _criteria_from_html(soup: BeautifulSoup) -> dict[str, str]:
    """Read LinkedIn-style "job criteria" pairs (Seniority level, Job function…).

    JSON-LD has no seniority field, but LinkedIn renders it in the page body,
    and it is the single field the profile filter cares about most.
    """
    criteria: dict[str, str] = {}
    for item in soup.select(".description__job-criteria-item"):
        label_el = item.select_one(".description__job-criteria-subheader")
        value_el = item.select_one(".description__job-criteria-text")
        if label_el and value_el:
            label = label_el.get_text(strip=True).rstrip(":").lower()
            value = value_el.get_text(strip=True)
            if label and value:
                criteria[label] = value
    return criteria


def fetch_job_detail(url: str, session: requests.Session | None = None) -> dict:
    """Fetch one posting page and return whatever detail it exposes.

    Returns an empty dict on any failure — enrichment is strictly additive and
    a blocked or moved posting must never cost us the list-level item.
    """
    getter = session or requests
    try:
        resp = getter.get(url, headers=_HEADERS, timeout=_REQUEST_TIMEOUT, allow_redirects=True)
        if resp.status_code >= 400:
            logger.debug("Detail fetch %s → HTTP %s", url, resp.status_code)
            return {}
        soup = BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        logger.debug("Detail fetch failed for %s: %s", url, exc)
        return {}
    except Exception as exc:
        logger.debug("Detail parse failed for %s: %s", url, exc)
        return {}

    detail: dict[str, str] = {}
    job = _find_job_posting(soup)

    if job:
        detail["company"] = _as_text(job.get("hiringOrganization"))
        detail["industry"] = _as_text(job.get("industry"))
        detail["location"] = _format_jsonld_location(job)
        detail["salary"] = _format_jsonld_salary(job)
        detail["date"] = _as_text(job.get("datePosted"))[:10]

        emp = _as_text(job.get("employmentType"))
        detail["employment_type"] = ", ".join(
            _EMPLOYMENT_TYPE_LABELS.get(part.strip().upper(), part.strip().title())
            for part in emp.split(",") if part.strip()
        )
        # description is HTML inside a JSON string — unescape, then strip tags.
        detail["description"] = _strip_html(html_lib.unescape(_as_text(job.get("description"))))

    criteria = _criteria_from_html(soup)
    if criteria.get("seniority level"):
        detail["seniority"] = criteria["seniority level"]
    if not detail.get("employment_type") and criteria.get("employment type"):
        detail["employment_type"] = criteria["employment type"]
    if not detail.get("industry") and criteria.get("industries"):
        detail["industry"] = criteria["industries"]

    if not detail.get("description"):
        # Common description containers, then the meta description as a floor.
        for sel in (".show-more-less-html__markup", "[class*='job-description']",
                    "#job-description", "article"):
            el = soup.select_one(sel)
            if el:
                text = _strip_html(el.decode_contents())
                if len(text) > 120:
                    detail["description"] = text
                    break
        else:
            meta = soup.find("meta", attrs={"name": "description"}) or \
                   soup.find("meta", attrs={"property": "og:description"})
            if meta and meta.get("content"):
                detail["description"] = _strip_html(meta["content"])

    return {k: v for k, v in detail.items() if v}


def enrich_with_detail(results: list[ScrapeResult], source: dict) -> None:
    """Fill in posting-page detail on `results`, in place.

    Only fields the list page left empty are overwritten, except location and
    date: JSON-LD gives a structured city/state and an ISO date, both of which
    beat a card's free text and its "2 weeks ago".
    """
    if _enrichment_time_left() <= 0:
        logger.warning(
            "Source %-30s → skipping detail enrichment (run-wide %.0fs budget spent); "
            "items keep their list-level fields.",
            source["id"], _ENRICHMENT_TIME_BUDGET_SEC,
        )
        return

    budget = min(len(results), _MAX_DETAIL_FETCHES_PER_SOURCE)
    if budget <= 0:
        return
    if len(results) > budget:
        logger.info(
            "Source %-30s → enriching first %d of %d items (per-source cap)",
            source["id"], budget, len(results),
        )

    enriched = 0
    with requests.Session() as session:
        for index, item in enumerate(results[:budget]):
            if not item.url.startswith("http"):
                continue
            if _enrichment_time_left() <= 0:
                logger.warning(
                    "Source %-30s → detail enrichment cut short at %d/%d "
                    "(run-wide budget spent).", source["id"], enriched, budget,
                )
                break
            if index:
                time.sleep(_DETAIL_FETCH_DELAY_SEC)  # be a polite client

            detail = fetch_job_detail(item.url, session=session)
            if not detail:
                continue

            for field in ("company", "salary", "employment_type", "seniority",
                          "industry", "description"):
                if detail.get(field) and not getattr(item, field):
                    setattr(item, field, detail[field])

            # Structured beats free-text, so these override rather than fill.
            if detail.get("location"):
                item.location = detail["location"]
            if detail.get("date"):
                item.date = detail["date"]
            if item.description and not item.snippet:
                item.snippet = _clip(item.description)
            enriched += 1

    logger.info("Source %-30s → detail enriched %d/%d items",
                source["id"], enriched, budget)
