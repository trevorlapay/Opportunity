"""
main.py — Orchestrator / entry point for Project Opportunity.

Pipeline per run:
  1. Load sources
  2. For each source: health-check → scrape → self-heal if needed
  3. Apply candidate profile filter
  4. Deduplicate
  5. If new items → send digest email
  6. Update run_log.json
"""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure src/ is on the path when running via Docker CMD
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
import deduplicator
import emailer
import filter as profile_filter
import healer
import scheduler
from scraper import scrape_source, check_url_health

logger = logging.getLogger(__name__)


# ── Run log ───────────────────────────────────────────────────────────────────

def _load_run_log() -> dict:
    try:
        with open(config.RUN_LOG_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"runs": []}


def _append_run_log(entry: dict) -> None:
    config.RUN_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    run_log = _load_run_log()
    run_log["runs"].append(entry)
    # Keep last 500 runs to prevent unbounded growth
    run_log["runs"] = run_log["runs"][-500:]
    with open(config.RUN_LOG_FILE, "w", encoding="utf-8") as fh:
        json.dump(run_log, fh, indent=2)


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run_pipeline() -> None:
    run_start = datetime.now(timezone.utc)
    run_entry: dict = {
        "timestamp": run_start.isoformat(),
        "sources_checked": 0,
        "sources_skipped_dead": 0,
        "sources_healthy": 0,
        "raw_items_found": 0,
        "new_items_after_filter": 0,
        "new_items_after_dedup": 0,
        "email_sent": False,
        "dead_sources": [],
        "errors": [],
    }

    # Reset per-run healer counters
    healer.reset_run_counters()

    # 1. Load sources
    try:
        sources = config.load_sources()
    except Exception as exc:
        logger.critical("Cannot load sources — aborting run: %s", exc)
        run_entry["errors"].append(str(exc))
        _append_run_log(run_entry)
        return

    all_raw_results = []

    run_entry.setdefault("auto_revived", [])

    for source in sources:
        source_id = source["id"]

        # Auto-revive: dead sources older than the revive window get another shot
        # (safety valve against the dead-is-forever trap).
        try:
            if healer.maybe_auto_revive(source, sources):
                run_entry["auto_revived"].append(source_id)
        except Exception as exc:
            logger.warning("Auto-revive check failed for %s: %s", source_id, exc)

        status = source.get("status", "healthy")

        # Dead sources are skipped — will be retried after auto-revive window
        if status in ("dead", "DEGRADED"):
            logger.debug("Skipping %s source: %s", status, source_id)
            run_entry["sources_skipped_dead"] += 1
            continue

        run_entry["sources_checked"] += 1

        # 2. Health check + healing (never raises)
        needs_heal = False
        try:
            needs_heal = healer.should_heal(source)
        except Exception as exc:
            logger.warning("Health check error on source %s: %s — skipping heal.", source_id, exc)

        if needs_heal:
            source = healer.heal_source(source, sources)
            if source["status"] in ("dead", "DEGRADED"):
                run_entry["sources_skipped_dead"] += 1
                run_entry["dead_sources"].append(source_id)
                continue

        run_entry["sources_healthy"] += 1

        # 3. Scrape (never raises past this block)
        try:
            results = scrape_source(source)
        except Exception as exc:
            logger.error("Scrape error on source %s: %s", source_id, exc, exc_info=True)
            run_entry["errors"].append(f"{source_id}: {exc}")
            results = []

        if not results:
            try:
                trigger = healer.increment_empty_run(source, sources)
            except Exception:
                trigger = False

            if trigger:
                source = healer.heal_source(source, sources)
                if source["status"] in ("dead", "DEGRADED"):
                    run_entry["sources_skipped_dead"] += 1
                    run_entry["dead_sources"].append(source_id)
                else:
                    try:
                        results = scrape_source(source)
                    except Exception:
                        pass
        else:
            try:
                healer.reset_empty_run(source, sources)
            except Exception:
                pass

        all_raw_results.extend(results)

    run_entry["raw_items_found"] = len(all_raw_results)
    logger.info("Total raw items scraped: %d", len(all_raw_results))

    # 4. Apply candidate profile filter (sources passed so remote-only-source
    #    items can bypass geography filtering — every item from a remote-only
    #    feed is by definition remote work, regardless of company HQ).
    filtered = profile_filter.apply_profile_filter(all_raw_results, sources)
    run_entry["new_items_after_filter"] = len(filtered)
    logger.info("After profile filter: %d items", len(filtered))

    # 5. Deduplicate
    new_items = deduplicator.get_new_items(filtered)
    run_entry["new_items_after_dedup"] = len(new_items)
    logger.info("Net-new items: %d", len(new_items))

    # 6. Send digest if new items found
    if new_items:
        new_items_as_dicts = [
            item.to_dict() if hasattr(item, "to_dict") else item
            for item in new_items
        ]
        success = emailer.send_digest(new_items_as_dicts, run_start)
        run_entry["email_sent"] = success
        if not success:
            logger.error("Failed to send digest email.")
    else:
        logger.info("No new items found — no email sent.")

    _append_run_log(run_entry)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    scheduler.run_forever(run_pipeline)
