"""
evaluator.py — Content-based opportunity scoring against USER_PREFS.md.

The keyword filter answers "does this title contain a senior-sounding word".
That is the wrong question: the function this candidate performs has no
consistent title, and the titles it does share (VP, Director, Head of) mean
something entirely different in banking and construction. This module asks the
right question instead — it reads the posting and scores it against the match
test in the brief.

Pipeline position: runs AFTER deduplication, so only net-new items are ever
sent to the model. That is the whole cost story.

Two outputs, never mixed:
  main      — the shortlist, scored on the Section 2 match test
  watchlist — infosec / data-center roles, diverted rather than dropped

Every failure mode degrades to "pass the items through unscored" rather than
losing them. A digest without annotations beats no digest.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
USER_PREFS_FILE = BASE_DIR / "USER_PREFS.md"

# Load .env here too. main.py imports config (which loads it) first, so this is
# redundant in the normal path — but without it, importing this module on its
# own degrades silently to unscored pass-through, which looks like a working
# run and is the worst failure mode this file has.
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

ENABLED = os.getenv("EVALUATOR_ENABLED", "1").strip().lower() not in ("0", "false", "no")
MODEL = os.getenv("EVALUATOR_MODEL", "claude-opus-5")
BATCH_SIZE = int(os.getenv("EVALUATOR_BATCH_SIZE", "6"))
MAX_ITEMS_PER_RUN = int(os.getenv("EVALUATOR_MAX_ITEMS", "120"))
MAIN_LIST_CAP = int(os.getenv("EVALUATOR_MAIN_CAP", "20"))
WATCHLIST_CAP = int(os.getenv("EVALUATOR_WATCHLIST_CAP", "20"))
DESCRIPTION_CHARS = int(os.getenv("EVALUATOR_DESC_CHARS", "3500"))

# The tool the model is forced to call. Forcing a tool is how we get reliable
# structured output on anthropic==0.49.0, which predates output_config.
_VERDICT_TOOL = {
    "name": "record_verdicts",
    "description": "Record one verdict for every posting in the batch.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {
                            "type": "integer",
                            "description": "The posting's index as given in the batch.",
                        },
                        "verdict": {
                            "type": "string",
                            "enum": ["main", "watchlist", "reject"],
                            "description": (
                                "main = passes the Section 2 match test. "
                                "watchlist = infosec/data-center per Section 6. "
                                "reject = fails the match test or hits a hard exclude."
                            ),
                        },
                        "signals": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "Which Section 2 match-test signals (1-6) this posting hits.",
                        },
                        "why_it_fits": {
                            "type": "string",
                            "description": "Two sentences maximum. Name the signals hit. Empty when rejected.",
                        },
                        "the_catch": {
                            "type": "string",
                            "description": "One sentence. The real risk. Required for main and watchlist.",
                        },
                        "ease_of_entry": {
                            "type": "string",
                            "enum": ["High", "Medium", "Low", ""],
                            "description": "High means the resume maps directly with no translation.",
                        },
                        "ease_reason": {"type": "string", "description": "One line."},
                        "who_to_call": {
                            "type": "string",
                            "description": "A specific person, org, or network path. 'cold' if none findable.",
                        },
                        "signal_read": {
                            "type": "string",
                            "description": "Watchlist only: one line on what this signals about the industry.",
                        },
                        "reject_reason": {
                            "type": "string",
                            "description": "Reject only: which hard exclude or missing signal killed it.",
                        },
                    },
                    "required": ["index", "verdict"],
                },
            }
        },
        "required": ["verdicts"],
    },
}

_INSTRUCTIONS = """You are screening job postings against the standing brief above.

For each posting in the batch, call record_verdicts once with one entry per posting.

Decide the verdict in this order:
1. Does it hit a HARD EXCLUDE (Section 4)? Judge by what the role actually is,
   not by its title. A "Vice President" doing commercial lending or trust
   administration is financial services and is excluded. A "General Manager"
   running a hotel is hospitality and is excluded. If so: reject.
2. Is it information security, cybersecurity, digital infrastructure, or data
   centers (Section 6)? If it also plausibly fits the operating profile:
   watchlist. If it requires hands-on technical depth or is below Senior
   Director scope: reject.
3. Otherwise score it against the Section 2 match test. Three or more signals:
   main. Fewer than three: reject.

Be strict. The previous keyword system returned 614 items of which about 5 were
relevant. A short honest list is the goal. Reject anything you would not defend.

Judge only on what the posting actually says. Where a description is missing or
truncated, say so in the_catch rather than inventing detail.

Every main and watchlist item needs the_catch filled in. If you cannot find a
real risk, you have not read the posting closely enough.

No em dashes in any field."""


def _load_brief() -> str:
    try:
        return USER_PREFS_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.error("Cannot read %s: %s", USER_PREFS_FILE, exc)
        return ""


def _render_batch(items: list, offset: int) -> str:
    """Render postings as numbered blocks for the model to score."""
    blocks = []
    for local_index, item in enumerate(items):
        description = (item.description or item.snippet or "").strip()
        if len(description) > DESCRIPTION_CHARS:
            description = description[:DESCRIPTION_CHARS] + " ...[truncated]"
        blocks.append(
            f"### POSTING {offset + local_index}\n"
            f"Title: {item.title or '(none)'}\n"
            f"Organization: {item.company or '(not stated)'}\n"
            f"Location: {item.location or '(not stated)'}\n"
            f"Employment type: {item.employment_type or '(not stated)'}\n"
            f"Seniority (as stated by source): {item.seniority or '(not stated)'}\n"
            f"Industry (as stated by source): {item.industry or '(not stated)'}\n"
            f"Salary: {item.salary or '(not stated)'}\n"
            f"Posted: {item.date or '(not stated)'}\n"
            f"Link: {item.url}\n"
            f"Description:\n{description or '(no description scraped)'}\n"
        )
    return "\n".join(blocks)


def _score_batch(client, brief: str, items: list, offset: int) -> dict:
    """Return {absolute_index: verdict_dict} for one batch. {} on failure."""
    import anthropic

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=8192,
            system=[
                # The brief is identical on every batch and every run, so cache
                # it rather than re-billing ~4k tokens per call.
                {"type": "text", "text": brief, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": _INSTRUCTIONS},
            ],
            tools=[_VERDICT_TOOL],
            tool_choice={"type": "tool", "name": "record_verdicts"},
            messages=[{"role": "user", "content": _render_batch(items, offset)}],
        )
    except anthropic.APIError as exc:
        logger.error("Evaluator API error on batch at %d: %s", offset, exc)
        return {}
    except Exception as exc:
        logger.error("Evaluator unexpected error on batch at %d: %s", offset, exc)
        return {}

    for block in response.content:
        if getattr(block, "type", "") == "tool_use":
            verdicts = (block.input or {}).get("verdicts", [])
            out = {}
            for v in verdicts:
                if isinstance(v, dict) and "index" in v:
                    try:
                        out[int(v["index"])] = v
                    except (TypeError, ValueError):
                        continue
            return out

    logger.error("Evaluator returned no tool_use block for batch at %d.", offset)
    return {}


def evaluate(items: list) -> tuple:
    """Score `items` against the brief.

    Returns (main, watchlist, stats, processed). `processed` is every item
    this call actually judged — main + watchlist + rejected + the unscored
    fallback — i.e. `items[:MAX_ITEMS_PER_RUN]` in every path below. It is
    NOT the same as `items`: whatever the per-run cap left untouched is
    excluded on purpose, because that's exactly what the caller must NOT
    hand to deduplicator.mark_seen() — an unjudged item marked seen vanishes
    from every future run too. Each returned item carries an `evaluation`
    attribute holding the verdict dict.

    On any failure the items are returned unscored in `main` rather than
    dropped, with stats["degraded"] set so the digest can say so.
    """
    stats = {"evaluated": 0, "main": 0, "watchlist": 0, "rejected": 0,
             "skipped": 0, "degraded": False}
    if not items:
        return [], [], stats, []

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    brief = _load_brief()

    if not ENABLED or not api_key or not brief:
        reason = ("disabled by EVALUATOR_ENABLED" if not ENABLED else
                  "no ANTHROPIC_API_KEY" if not api_key else "no USER_PREFS.md")
        logger.warning("Evaluator not running (%s) — passing %d items through unscored.",
                       reason, len(items))
        stats["degraded"] = True
        # No cap applied in this path — everything was "processed" (shown to
        # the user unscored), so it's safe to mark all of it seen.
        return list(items), [], stats, list(items)

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    queue = items[:MAX_ITEMS_PER_RUN]
    if len(items) > MAX_ITEMS_PER_RUN:
        stats["skipped"] = len(items) - MAX_ITEMS_PER_RUN
        logger.warning(
            "Evaluator cap: scoring %d of %d new items this run (EVALUATOR_MAX_ITEMS).",
            len(queue), len(items),
        )

    verdicts = {}
    for start in range(0, len(queue), BATCH_SIZE):
        batch = queue[start:start + BATCH_SIZE]
        logger.info("Evaluating items %d-%d of %d ...",
                    start + 1, start + len(batch), len(queue))
        verdicts.update(_score_batch(client, brief, batch, start))

    if not verdicts:
        logger.error("Evaluator produced no verdicts — passing items through unscored.")
        stats["degraded"] = True
        return list(queue), [], stats, list(queue)

    main, watchlist = [], []
    for index, item in enumerate(queue):
        verdict = verdicts.get(index)
        if verdict is None:
            # A dropped batch must not silently delete opportunities.
            logger.warning("No verdict for item %d (%s) — keeping it unscored.",
                           index, item.title)
            item.evaluation = {"verdict": "main", "unscored": True}
            main.append(item)
            continue

        stats["evaluated"] += 1
        item.evaluation = verdict
        kind = verdict.get("verdict", "reject")
        if kind == "main":
            main.append(item)
        elif kind == "watchlist":
            watchlist.append(item)
        else:
            stats["rejected"] += 1
            logger.debug("REJECTED %s — %s", item.title, verdict.get("reject_reason", ""))

    # Strongest first, so a cap trims the weakest rather than the newest.
    main.sort(key=lambda i: len(getattr(i, "evaluation", {}).get("signals") or []),
              reverse=True)

    if len(main) > MAIN_LIST_CAP:
        logger.info("Main list capped at %d (had %d).", MAIN_LIST_CAP, len(main))
        main = main[:MAIN_LIST_CAP]
    if len(watchlist) > WATCHLIST_CAP:
        watchlist = watchlist[:WATCHLIST_CAP]

    stats["main"], stats["watchlist"] = len(main), len(watchlist)
    logger.info("Evaluator: %d scored -> %d main, %d watchlist, %d rejected.",
                stats["evaluated"], stats["main"], stats["watchlist"], stats["rejected"])
    if stats["skipped"]:
        logger.info(
            "Evaluator: %d item(s) left unjudged by this run's cap — NOT marked "
            "seen, so they remain candidates for the next run.", stats["skipped"],
        )
    # `queue` — pre-MAIN_LIST_CAP/WATCHLIST_CAP trim — not `main`/`watchlist`
    # after trimming: an item judged "main" but bumped by the display cap was
    # still judged, and re-scoring it every future run would just burn tokens
    # to reach the same verdict.
    return main, watchlist, stats, queue
