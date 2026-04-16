"""
healer.py — Self-healing pipeline for broken/degraded data sources.

Flow when a source is broken:
  1. Quick retry  — one fast re-check in case it was a transient blip.
  2. Alternate URLs — try any configured alternate_urls from sources.json.
  3. LLM lookup   — single, token-light Claude call: "Do you know a current
                    URL for this source?"  No web search, no agentic loops.
                    If Claude knows one → validate by scraping → swap in.
                    If Claude doesn't know → mark source "dead" (never retry).

"Dead" sources stay in sources.json for auditability but are permanently
skipped by the pipeline.  The only way to revive one is a manual
`update-source <id> status healthy` via the management CLI.
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone

import anthropic

import config
from scraper import check_url_health, scrape_source

logger = logging.getLogger(__name__)

CONSECUTIVE_EMPTY_RUNS_THRESHOLD = 3

# ── Anthropic client (lazy) ────────────────────────────────────────────────────
_anthropic_client: anthropic.Anthropic | None = None

# ── LLM rate limiting (simple) ────────────────────────────────────────────────
_LLM_MIN_INTERVAL_SEC = 10   # minimum seconds between consecutive LLM calls
_LLM_MAX_PER_RUN = 5         # cap per pipeline run (each call is cheap but burst-prone)
_LLM_HEAL_COOLDOWN_DAYS = 7  # don't attempt LLM heal for a source more than once per week
_llm_last_call_time: float = 0.0
_llm_calls_this_run: int = 0


def reset_run_counters() -> None:
    """Call once at the start of each pipeline run to reset per-run state."""
    global _llm_calls_this_run
    _llm_calls_this_run = 0


def _get_anthropic_client() -> anthropic.Anthropic | None:
    global _anthropic_client
    if not config.ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — LLM healing disabled.")
        return None
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _anthropic_client


# ── Public entry point ────────────────────────────────────────────────────────

def heal_source(source: dict, sources: list[dict]) -> dict:
    """
    Run the healing pipeline for a broken source.  NEVER raises.

    Returns the (possibly mutated) source dict.
    On success: source["status"] == "healthy", active_url updated.
    On failure: source["status"] == "dead"  — skipped by pipeline forever.
    """
    source_id = source["id"]
    logger.warning("Self-healing triggered for source: %s", source_id)

    try:
        # Step 1 — Quick retry (catches transient network blips)
        if _quick_retry(source):
            source["status"] = "healthy"
            source["last_verified"] = _now()
            source["consecutive_empty_runs"] = 0
            _persist(sources)
            logger.info("Source %s recovered on quick retry.", source_id)
            return source

        # Step 2 — Alternate URLs (free, no API calls)
        recovered_url = _try_alternate_urls(source)
        if recovered_url:
            source["active_url"] = recovered_url
            source["status"] = "healthy"
            source["last_verified"] = _now()
            source["consecutive_empty_runs"] = 0
            _persist(sources)
            logger.info("Source %s recovered via alternate URL: %s", source_id, recovered_url)
            return source

        # Step 3 — Ask the LLM once: does it know a better URL?
        llm_ran = False
        new_url, new_api_cfg = None, None
        try:
            new_url, new_api_cfg, llm_ran = _llm_lookup(source)
        except Exception as exc:
            logger.warning("LLM lookup error for %s: %s — will retry next run.", source_id, exc)

        if new_url:
            source["active_url"] = new_url
            if new_api_cfg:
                source.setdefault("api_config", {}).update(new_api_cfg)
            source["status"] = "healthy"
            source["last_verified"] = _now()
            source["consecutive_empty_runs"] = 0
            _persist(sources)
            logger.info("Source %s healed via LLM suggestion: %s", source_id, new_url)
            return source

        if not llm_ran:
            # Cap hit or rate-limited — don't penalise the source, try again next run
            logger.info(
                "Source %s: LLM lookup was skipped this run (cap/rate-limit) — "
                "will retry next run.",
                source_id,
            )
            return source

        # Step 4 — LLM ran and found nothing: mark dead, do not retry
        source["status"] = "dead"
        source["last_verified"] = _now()
        _persist(sources)
        logger.error(
            "Source %s marked dead — URL is broken and LLM knows no replacement. "
            "Permanently skipped. Use manage.py update-source to revive manually.",
            source_id,
        )

    except Exception as exc:
        logger.error(
            "Healing pipeline raised an unexpected error for source %s: %s — "
            "source left in current state, pipeline continues.",
            source_id, exc, exc_info=True,
        )

    return source


def should_heal(source: dict) -> bool:
    """
    Return True if this source needs healing this run.

    Heals on: connection failure (0), URL not found (404), server errors (5xx).
    Skips on:
      - "dead" or "DEGRADED" status (already permanently failed)
      - 401/403/429 (bot-blocking — URL is fine, just rejecting us)
      - workday_api strategy (portal URL != API endpoint; use empty-run counter)
    """
    status = source.get("status", "healthy")
    if status in ("dead", "DEGRADED"):
        return False

    if source.get("strategy") == "workday_api":
        return False

    is_healthy, code = check_url_health(source["active_url"])
    if is_healthy:
        return False

    if code in (401, 403, 429):
        logger.debug(
            "Source %s: HTTP %s (bot-block/auth) — skipping heal, "
            "accumulating toward empty-run threshold.",
            source["id"], code,
        )
        return False

    logger.info("Source %s: URL returned %s — healing needed.", source["id"], code)
    return True


def increment_empty_run(source: dict, sources: list[dict]) -> bool:
    """
    Increment consecutive_empty_runs.
    Returns True when threshold is reached and healing should be triggered.
    """
    source["consecutive_empty_runs"] = source.get("consecutive_empty_runs", 0) + 1
    if source["consecutive_empty_runs"] >= CONSECUTIVE_EMPTY_RUNS_THRESHOLD:
        logger.warning(
            "Source %s: %d consecutive empty runs — triggering healing.",
            source["id"], source["consecutive_empty_runs"],
        )
        _persist(sources)
        return True
    _persist(sources)
    return False


def reset_empty_run(source: dict, sources: list[dict]) -> None:
    if source.get("consecutive_empty_runs", 0) > 0:
        source["consecutive_empty_runs"] = 0
        _persist(sources)


# ── Step implementations ───────────────────────────────────────────────────────

def _quick_retry(source: dict) -> bool:
    """One immediate re-check — catches transient DNS/network blips."""
    time.sleep(5)
    is_healthy, code = check_url_health(source["active_url"])
    if not is_healthy:
        logger.info("Quick retry: still unhealthy (code %s) for %s.", code, source["id"])
        return False
    results = scrape_source(source)
    if results:
        return True
    logger.info("Quick retry: URL healthy but no content for %s.", source["id"])
    return False


def _try_alternate_urls(source: dict) -> str | None:
    """Try each alternate_url. Returns the first that is healthy AND yields content."""
    for alt_url in source.get("alternate_urls", []):
        logger.info("Trying alternate URL for %s: %s", source["id"], alt_url)
        is_healthy, code = check_url_health(alt_url)
        if not is_healthy:
            logger.info("  Alternate unhealthy (%s): %s", code, alt_url)
            continue
        test = {**source, "active_url": alt_url}
        if scrape_source(test):
            return alt_url
        logger.info("  Alternate healthy but no content: %s", alt_url)
    return None


def _llm_lookup(source: dict) -> tuple[str | None, dict | None, bool]:
    """
    Single token-light Claude call: does the model know a current URL?

    Returns (new_active_url, api_config_updates, llm_ran).
    llm_ran=False means the call was skipped (cap/no key) — don't penalise source.
    llm_ran=True means Claude was consulted (even if it found nothing).
    """
    global _llm_last_call_time, _llm_calls_this_run

    client = _get_anthropic_client()
    if client is None:
        return None, None, False

    if _llm_calls_this_run >= _LLM_MAX_PER_RUN:
        logger.warning(
            "LLM heal cap reached (%d/%d) — skipping %s this run.",
            _llm_calls_this_run, _LLM_MAX_PER_RUN, source["id"],
        )
        return None, None, False

    # Weekly cooldown: don't burn tokens re-checking a source we already tried recently
    last_heal_str = source.get("last_llm_heal_ts")
    if last_heal_str:
        try:
            last_heal = datetime.fromisoformat(last_heal_str)
            if datetime.now(timezone.utc) - last_heal < timedelta(days=_LLM_HEAL_COOLDOWN_DAYS):
                logger.info(
                    "Source %s: LLM heal attempted %s ago — cooling down for %d days, skipping.",
                    source["id"],
                    str(datetime.now(timezone.utc) - last_heal).split(".")[0],
                    _LLM_HEAL_COOLDOWN_DAYS,
                )
                return None, None, False
        except ValueError:
            pass  # malformed timestamp — proceed with the call

    # Enforce minimum inter-call gap
    elapsed = time.monotonic() - _llm_last_call_time
    if elapsed < _LLM_MIN_INTERVAL_SEC:
        time.sleep(_LLM_MIN_INTERVAL_SEC - elapsed)

    strategy = source.get("strategy", "")
    api_cfg = source.get("api_config", {})

    if strategy == "workday_api":
        extra = (
            f"\nLast known Workday base URL: {api_cfg.get('base_url', source['active_url'])}\n"
            f"Last known endpoint: {api_cfg.get('endpoint', '')}\n"
            f"Workday pattern: https://{{tenant}}.wd{{N}}.myworkdayjobs.com  "
            f"/ /wday/cxs/{{tenant}}/{{Site}}/jobs"
        )
        json_schema = (
            '{"replacement_url": "https://..." or null, '
            '"workday_base_url": "https://..." or null, '
            '"workday_endpoint": "/wday/cxs/..." or null, '
            '"notes": "..."}'
        )
    else:
        extra = ""
        json_schema = '{"replacement_url": "https://..." or null, "notes": "..."}'

    prompt = (
        f"Source: {source['name']} ({source['category']})\n"
        f"Broken URL: {source['active_url']}{extra}\n\n"
        f"Do you know a current, working replacement URL for this source? "
        f"Answer with JSON only — no other text:\n{json_schema}"
    )

    logger.info("LLM heal lookup for %s …", source["id"])
    _llm_last_call_time = time.monotonic()
    _llm_calls_this_run += 1
    source["last_llm_heal_ts"] = _now()  # stamp before the call so cooldown applies even on error

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip() if response.content else ""
        logger.debug("LLM heal raw: %s", raw)

        # Tolerate markdown fences and leading/trailing text
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start < 0 or end <= start:
            logger.warning("LLM heal: no JSON found in response for %s.", source["id"])
            return None, None, True  # LLM ran but response unparseable
        parsed = json.loads(raw[start:end])

        replacement: str | None = parsed.get("replacement_url") or None
        notes: str = parsed.get("notes", "")

        if replacement and not replacement.startswith("http"):
            replacement = None

        logger.info(
            "LLM heal result for %s: replacement=%s notes=%s",
            source["id"], replacement, notes,
        )

        if not replacement:
            return None, None, True  # LLM ran, explicitly has no replacement

        # Validate: scrape is the definitive test
        api_updates: dict | None = None
        if strategy == "workday_api":
            new_base = (parsed.get("workday_base_url") or "").strip()
            new_ep = (parsed.get("workday_endpoint") or "").strip()
            if new_base and new_ep:
                api_updates = {"base_url": new_base, "endpoint": new_ep}
                logger.info("LLM heal: Workday config → base=%s ep=%s", new_base, new_ep)

        test = {**source, "active_url": replacement}
        if api_updates:
            test["api_config"] = {**source.get("api_config", {}), **api_updates}

        results = scrape_source(test)
        if results:
            logger.info("LLM heal validated: %d items from %s", len(results), replacement)
            return replacement, api_updates, True

        logger.warning(
            "LLM heal: suggested URL %s scraped 0 items for %s — treating as dead.",
            replacement, source["id"],
        )
        return None, None, True  # LLM ran, suggested URL validated to 0 items

    except anthropic.RateLimitError as exc:
        logger.warning("LLM heal rate-limited for %s: %s", source["id"], exc)
        return None, None, False  # transient — don't penalise source
    except json.JSONDecodeError as exc:
        logger.error("LLM heal JSON parse error for %s: %s | raw=%r", source["id"], exc, raw)
        return None, None, True  # LLM ran but we couldn't parse the response
    except anthropic.APIError as exc:
        logger.error("LLM heal API error for %s: %s", source["id"], exc)
        return None, None, False  # transient API failure — don't penalise source


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _persist(sources: list[dict]) -> None:
    config.save_sources(sources)
