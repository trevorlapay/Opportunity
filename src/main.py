"""
main.py — Orchestrator / entry point for Project Opportunity.

Pipeline per run:
  1. Load sources
  2. For each source: health-check → scrape (+ enrich) → self-heal if needed
  3. Apply the keyword gate (geography, hard excludes, entry-level floor)
  4. Deduplicate
  5. Score net-new items against USER_PREFS.md → main list + watchlist
  6. Synthesise the closing sections (what changed, act on, pattern flag)
  7. If anything scored → send digest email
  8. Update run_log.json

Steps 3 and 5 do different jobs. Step 3 is cheap and coarse: it exists to keep
step 5's cost bounded, not to decide relevance. Step 5 reads each posting and
makes the actual judgment, because the brief's central point is that the title
is the worst available signal for this search.
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
import evaluator
import filter as profile_filter
import healer
import scheduler
import scraper
import synthesis
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


def _previous_run_timestamp() -> str | None:
    """ISO timestamp of the last completed run, or None on the first ever run."""
    runs = _load_run_log().get("runs", [])
    return runs[-1].get("timestamp") if runs else None


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
        "evaluated": 0,
        "main_list": 0,
        "watchlist": 0,
        "rejected_by_evaluator": 0,
        "email_sent": False,
        "dead_sources": [],
        "errors": [],
    }

    # Reset per-run healer counters and start the enrichment time budget.
    healer.reset_run_counters()
    scraper.begin_run()

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

        # Only dead sources are skipped, and only until the auto-revive window.
        # Degraded ones are suspect but still scraped — that intermediate state
        # exists so a source gets more than one bad day before it's retired.
        if healer.is_dead(source):
            logger.debug("Skipping dead source: %s", source_id)
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
            if healer.is_dead(source):
                run_entry["sources_skipped_dead"] += 1
                run_entry["dead_sources"].append(source_id)
                continue

        run_entry["sources_healthy"] += 1

        # 3. Scrape (never raises past this block).
        #    enrich=True fetches each job posting page for company, salary,
        #    seniority, and description — the fields a list card never carries.
        try:
            results = scrape_source(source, enrich=True)
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
                if healer.is_dead(source):
                    run_entry["sources_skipped_dead"] += 1
                    run_entry["dead_sources"].append(source_id)
                else:
                    try:
                        results = scrape_source(source, enrich=True)
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

    # 5. Find net-new candidates. Deliberately NOT persisted yet — see step 6.
    new_items = deduplicator.find_new(filtered)
    run_entry["new_items_after_dedup"] = len(new_items)
    logger.info("Net-new items: %d", len(new_items))

    # 6. Score the net-new items against USER_PREFS.md.
    #    This runs after dedup so only genuinely new postings cost a model call,
    #    and it is what turns a keyword sweep into an actual shortlist: it reads
    #    each posting and judges what the role IS, not what it was called.
    #
    #    evaluator.evaluate() caps how many candidates it judges per run
    #    (EVALUATOR_MAX_ITEMS) and returns `processed` — the subset it actually
    #    looked at. Only THAT gets marked seen. A candidate left over from the
    #    cap stays a live candidate and is offered again next run instead of
    #    being silently discarded before anyone — human or model — judged it.
    main_list, watchlist, eval_stats, processed = evaluator.evaluate(new_items)
    deduplicator.mark_seen(processed)
    run_entry["evaluated"] = eval_stats["evaluated"]
    run_entry["main_list"] = eval_stats["main"]
    run_entry["watchlist"] = eval_stats["watchlist"]
    run_entry["rejected_by_evaluator"] = eval_stats["rejected"]
    run_entry["evaluator_degraded"] = eval_stats["degraded"]
    if eval_stats["skipped"]:
        run_entry["evaluator_skipped"] = eval_stats["skipped"]

    # 7. Closing sections: what changed, the one thing to act on, the pattern
    #    flag. The flag compares opportunities reviewed against conversations
    #    initiated since the PREVIOUS run, so it needs that run's timestamp.
    try:
        summary = synthesis.summarize(main_list, watchlist, eval_stats,
                                      last_run_iso=_previous_run_timestamp())
    except Exception as exc:
        logger.error("Synthesis failed: %s — digest will omit closing sections.", exc)
        summary = {}
    run_entry["conversations_since_last_run"] = summary.get("conversations_since_last_run", 0)

    # 8. Send digest if anything survived scoring
    if main_list or watchlist:
        to_dicts = lambda items: [
            item.to_dict() if hasattr(item, "to_dict") else item for item in items
        ]
        success = emailer.send_digest(
            to_dicts(main_list), run_start,
            watchlist=to_dicts(watchlist), eval_stats=eval_stats, summary=summary,
        )
        run_entry["email_sent"] = success
        if not success:
            logger.error("Failed to send digest email.")
    elif new_items:
        logger.info("%d new items, none passed the match test — no email sent.",
                    len(new_items))
    else:
        logger.info("No new items found — no email sent.")

    _append_run_log(run_entry)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    scheduler.run_forever(run_pipeline)
