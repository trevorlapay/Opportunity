"""
deduplicator.py — Fingerprint-based deduplication and seen-items state management.

Reads/writes data/seen_items.json.  Items older than SEEN_ITEM_RETENTION_DAYS
are purged so that reposted jobs can resurface after the retention window.

Records written before the schema widened carry only the identity fields;
nothing here reads the newer keys, so old and new records coexist fine.

Discovery and persistence are deliberately two separate calls — find_new()
then mark_seen() — not one. evaluator.py caps how many net-new candidates it
actually judges in a single run (EVALUATOR_MAX_ITEMS); the ones past the cap
were never scored, and if this module marked every candidate seen at
discovery time, those un-judged candidates would vanish from every future
run's dedup pass too, permanently, without a human or the model ever having
looked at them. Call mark_seen() only with items that were actually
evaluated or otherwise surfaced — anything else stays a live candidate and
gets picked up again next run.
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import config

logger = logging.getLogger(__name__)

_EMPTY_STATE: dict = {"items": []}


# ── State I/O ─────────────────────────────────────────────────────────────────

def _load_seen() -> dict:
    path: Path = config.SEEN_ITEMS_FILE
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        logger.info("seen_items.json not found — starting fresh.")
        return {"items": []}
    except json.JSONDecodeError as exc:
        logger.critical(
            "seen_items.json is corrupted (%s) — rebuilding from empty state.", exc
        )
        return {"items": []}


def _save_seen(state: dict) -> None:
    config.SEEN_ITEMS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(config.SEEN_ITEMS_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


# ── Public API ────────────────────────────────────────────────────────────────

def find_new(candidates: list) -> list:
    """
    Given a list of ScrapeResult objects, return only those whose fingerprint
    has not been seen before. Read-only — does NOT persist anything. Purges
    expired items from an in-memory copy of the state so the "already seen"
    check reflects the retention window, but that purge is only written to
    disk the next time mark_seen() runs (or never, if nothing new is found —
    matching the old save-only-when-there's-something-to-save behaviour).

    Call mark_seen() with whichever of these were actually acted on.
    """
    state = _load_seen()
    state = _purge_expired(state)
    seen_fps: set[str] = {item["fingerprint"] for item in state["items"]}
    new_items = [r for r in candidates if r.fingerprint not in seen_fps]

    logger.info("Deduplicator: %d new item(s) out of %d candidates.",
               len(new_items), len(candidates))
    return new_items


def mark_seen(items: list) -> None:
    """
    Persist `items` as seen.

    Call this ONLY for items that were actually evaluated or otherwise
    surfaced this run. An item find_new() returned but that nothing looked at
    (dropped by the evaluator's per-run cap, for instance) must NOT be passed
    here — doing so is how a busy run permanently erases candidates nobody
    ever judged. Leaving it out costs nothing: find_new() will offer it again
    next run.
    """
    if not items:
        return

    state = _load_seen()
    state = _purge_expired(state)
    seen_fps: set[str] = {item["fingerprint"] for item in state["items"]}
    now_iso = datetime.now(timezone.utc).isoformat()

    added = 0
    for result in items:
        fp = result.fingerprint
        if fp in seen_fps:
            continue
        # Keep the detail we scraped rather than just the identity fields —
        # otherwise the store can't answer "what was that role?" and a past
        # digest can never be re-rendered.
        #
        # The full `description` is deliberately not persisted: it is the one
        # unbounded field, the digest only ever shows `snippet`, and keeping it
        # would grow this file by an order of magnitude for no read path.
        seen_fps.add(fp)
        state["items"].append(
            {
                "fingerprint": fp,
                "title": result.title,
                "source_id": result.source_id,
                "url": result.url,
                "first_seen": now_iso,
                "category": result.category,
                "company": result.company,
                "location": result.location,
                "date": result.date,
                "salary": result.salary,
                "employment_type": result.employment_type,
                "seniority": result.seniority,
                "industry": result.industry,
                "snippet": result.snippet,
            }
        )
        added += 1

    if added:
        _save_seen(state)
        logger.info("Deduplicator: marked %d item(s) as seen.", added)


def _purge_expired(state: dict) -> dict:
    retention_days = config.SEEN_ITEM_RETENTION_DAYS
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    before = len(state["items"])
    state["items"] = [
        item
        for item in state["items"]
        if _parse_iso(item.get("first_seen", "")) >= cutoff
    ]
    purged = before - len(state["items"])
    if purged:
        logger.info("Purged %d expired item(s) (retention=%d days).", purged, retention_days)
    return state


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO 8601 timestamp, falling back to epoch on parse failure."""
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)
