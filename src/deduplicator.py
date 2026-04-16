"""
deduplicator.py — Fingerprint-based deduplication and seen-items state management.

Reads/writes data/seen_items.json.  Items older than SEEN_ITEM_RETENTION_DAYS
are purged so that reposted jobs can resurface after the retention window.
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

def get_new_items(candidates: list) -> list:
    """
    Given a list of ScrapeResult objects, return only those whose fingerprint
    has not been seen before.  Automatically purges expired items from the
    store and saves the updated state.
    """
    state = _load_seen()
    state = _purge_expired(state)

    seen_fps: set[str] = {item["fingerprint"] for item in state["items"]}

    new_items = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for result in candidates:
        fp = result.fingerprint
        if fp in seen_fps:
            continue
        # Mark as seen
        seen_fps.add(fp)
        state["items"].append(
            {
                "fingerprint": fp,
                "title": result.title,
                "source_id": result.source_id,
                "url": result.url,
                "first_seen": now_iso,
                "category": result.category,
            }
        )
        new_items.append(result)

    if new_items:
        _save_seen(state)
        logger.info("Deduplicator: %d new item(s) out of %d candidates.", len(new_items), len(candidates))
    else:
        logger.info("Deduplicator: no new items (all %d already seen).", len(candidates))

    return new_items


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
