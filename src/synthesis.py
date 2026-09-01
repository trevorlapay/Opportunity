"""
synthesis.py — The run-level closing sections of the digest.

The per-item annotations answer "is this one worth reading". These answer
"what do I do now", which is a different question and the one the brief ends on:

  1. What changed since last run.
  2. The one item to act on this week, with a person to contact rather than an
     application to submit.
  3. The pattern flag: opportunities reviewed against conversations initiated.
  4. A short industry read at the top of the watchlist, which the brief calls
     the actual point of that list.

The pattern flag is computed in code, not written by the model. The brief says
"do not soften it and do not lecture", and a deterministic sentence built from
two integers cannot drift, hedge, or editorialise. Everything else needs
judgment, so it goes to the model.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
OUTREACH_FILE = BASE_DIR / "data" / "outreach_log.json"

# Same reasoning as evaluator.py: without this, importing the module outside
# main.py degrades silently to "pattern flag only", which looks like a working
# run rather than a missing credential.
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

MODEL = os.getenv("SYNTHESIS_MODEL", os.getenv("EVALUATOR_MODEL", "claude-opus-5"))

_SUMMARY_TOOL = {
    "name": "record_summary",
    "description": "Record the closing sections of the digest.",
    "input_schema": {
        "type": "object",
        "properties": {
            "what_changed": {
                "type": "string",
                "description": (
                    "Two to four sentences on what is new in this run and how it "
                    "differs from a typical run. Lead with the answer. If the run "
                    "is thin, say so plainly rather than padding."
                ),
            },
            "act_on_title": {
                "type": "string",
                "description": "Title of the ONE item to act on this week. Must come from the MAIN LIST.",
            },
            "act_on_organization": {"type": "string"},
            "act_on_first_move": {
                "type": "string",
                "description": (
                    "The specific first move. It must be a person or a named network "
                    "path to contact, not an application to submit. One or two sentences."
                ),
            },
            "act_on_why": {
                "type": "string",
                "description": "One sentence on why this one beats the rest of the main list.",
            },
            "act_on_is_watchlist": {
                "type": "boolean",
                "description": (
                    "True ONLY if a watchlist role genuinely beats everything in the "
                    "main list. The brief forbids this by default; if you set it true "
                    "you must justify it in act_on_why."
                ),
            },
            "industry_read": {
                "type": "string",
                "description": (
                    "Two or three sentences for the top of the watchlist. What is the "
                    "market doing. Are these roles growing, shrinking, or changing "
                    "shape. Is the chief-of-staff and operating layer inside security "
                    "organizations expanding or getting absorbed. Empty string if the "
                    "watchlist is empty."
                ),
            },
        },
        "required": ["what_changed"],
    },
}

_INSTRUCTIONS = """You are writing the closing sections of an opportunity digest,
against the standing brief above.

You are given this run's main list and watchlist, already scored. Call
record_summary exactly once.

Rules that matter:
- The one item to act on comes from the MAIN LIST. The watchlist is never the
  source of it. If a watchlist role is genuinely compelling enough to beat
  everything in the main list, set act_on_is_watchlist true and justify it.
- The first move is a person to contact or a named network path. Never "apply
  on the careers page".
- Do not pad. If the run is thin, say the run is thin.
- Separate what a posting says from what you inferred. Label inferences.
- Short sentences. Lead with the answer. No em dashes."""


# ── Outreach log ──────────────────────────────────────────────────────────────

def _load_outreach() -> list:
    try:
        return json.loads(OUTREACH_FILE.read_text(encoding="utf-8")).get("entries", [])
    except (OSError, json.JSONDecodeError):
        return []


def log_outreach(who: str, note: str = "") -> dict:
    """Append one initiated conversation. Called by manage.py log-outreach."""
    entries = _load_outreach()
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "who": who.strip(),
        "note": note.strip(),
    }
    entries.append(entry)
    OUTREACH_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTREACH_FILE.write_text(json.dumps({"entries": entries}, indent=2), encoding="utf-8")
    return entry


def _parse_iso(value: str):
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def outreach_since(since_iso: str | None) -> int:
    """Count conversations initiated since `since_iso` (all of them if None)."""
    entries = _load_outreach()
    cutoff = _parse_iso(since_iso) if since_iso else None
    if cutoff is None:
        return len(entries)
    return sum(1 for e in entries
               if (_parse_iso(e.get("ts", "")) or datetime.min.replace(tzinfo=timezone.utc)) > cutoff)


def build_pattern_flag(reviewed: int, conversations: int) -> str:
    """The blunt one-liner. Computed, never generated.

    The brief asks for this explicitly and asks that it not be softened, so it
    is two integers and a statement, with no hedging language available to it.
    """
    if reviewed == 0 and conversations == 0:
        return "No opportunities reviewed and no conversations started since the last run."
    if conversations == 0:
        return (f"You reviewed {reviewed} opportunit{'y' if reviewed == 1 else 'ies'} "
                f"since the last run and started 0 conversations.")
    if reviewed > conversations:
        return (f"You reviewed {reviewed} opportunities since the last run and started "
                f"{conversations} conversation{'' if conversations == 1 else 's'}. "
                f"Reviewing is outpacing outreach.")
    return (f"You reviewed {reviewed} opportunit{'y' if reviewed == 1 else 'ies'} since the "
            f"last run and started {conversations} conversation"
            f"{'' if conversations == 1 else 's'}. Outreach is keeping pace.")


# ── Model-written sections ────────────────────────────────────────────────────

def _render_lists(main: list, watchlist: list) -> str:
    def block(items, label):
        if not items:
            return f"{label}: (empty this run)\n"
        lines = [f"{label} ({len(items)} items):"]
        for item in items:
            ev = getattr(item, "evaluation", None) or {}
            lines.append(
                f"  - {item.title} | {item.company or 'unknown org'} | "
                f"{item.location or 'location not stated'}\n"
                f"    signals: {ev.get('signals') or 'n/a'}\n"
                f"    why: {ev.get('why_it_fits', '')}\n"
                f"    catch: {ev.get('the_catch', '')}\n"
                f"    entry: {ev.get('ease_of_entry', '')} {ev.get('ease_reason', '')}\n"
                f"    contact path: {ev.get('who_to_call', '')}"
            )
        return "\n".join(lines) + "\n"

    return block(main, "MAIN LIST") + "\n" + block(watchlist, "WATCHLIST (infosec / data center)")


def summarize(main: list, watchlist: list, stats: dict,
              last_run_iso: str | None = None) -> dict:
    """Build the closing sections. Never raises; returns {} if unavailable."""
    reviewed = stats.get("evaluated", 0) or (len(main) + len(watchlist))
    conversations = outreach_since(last_run_iso)
    summary = {
        "pattern_flag": build_pattern_flag(reviewed, conversations),
        "conversations_since_last_run": conversations,
        "reviewed": reviewed,
    }

    if not main and not watchlist:
        return summary

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    try:
        import evaluator  # reuse the same brief loader, so they cannot diverge
        brief = evaluator._load_brief()
    except Exception:
        brief = ""
    if not api_key or not brief:
        logger.warning("Synthesis skipped (no API key or brief) — pattern flag only.")
        return summary

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=[
                {"type": "text", "text": brief, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": _INSTRUCTIONS},
            ],
            tools=[_SUMMARY_TOOL],
            tool_choice={"type": "tool", "name": "record_summary"},
            messages=[{"role": "user", "content": _render_lists(main, watchlist)}],
        )
    except Exception as exc:
        logger.error("Synthesis call failed: %s — pattern flag only.", exc)
        return summary

    payload = {}
    for block in response.content:
        if getattr(block, "type", "") == "tool_use":
            payload = block.input or {}
            break
    if not payload:
        logger.error("Synthesis returned no tool_use block — pattern flag only.")
        return summary

    summary["what_changed"] = payload.get("what_changed", "")
    summary["industry_read"] = payload.get("industry_read", "") if watchlist else ""

    title = (payload.get("act_on_title") or "").strip()
    if title:
        from_watchlist = bool(payload.get("act_on_is_watchlist"))
        # Guardrail from the brief: the watchlist is never the default source of
        # this pick. Enforce it rather than trusting the flag to be honest.
        main_titles = {i.title for i in main}
        if not from_watchlist and title not in main_titles and main:
            logger.warning(
                "Synthesis picked '%s', which is not in the main list and was not "
                "flagged as a watchlist override — dropping the pick.", title)
        else:
            summary["act_on"] = {
                "title": title,
                "organization": payload.get("act_on_organization", ""),
                "first_move": payload.get("act_on_first_move", ""),
                "why": payload.get("act_on_why", ""),
                "from_watchlist": from_watchlist,
                "url": next((i.url for i in list(main) + list(watchlist)
                             if i.title == title), ""),
            }

    return summary
