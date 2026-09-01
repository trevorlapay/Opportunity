"""
emailer.py — HTML email composition and SMTP delivery via Gmail.

Sends two types of emails:
  1. Digest email when net-new items are found.
  2. Alert email when a source self-heals to DEGRADED status.

Uses a Jinja2 template for the digest and inline text for alerts.
Retries once on SMTP failure (60-second cooldown).
"""

import logging
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

from jinja2 import Environment, FileSystemLoader, select_autoescape

import config

logger = logging.getLogger(__name__)

_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 587


def _build_smtp() -> smtplib.SMTP:
    smtp = smtplib.SMTP(_SMTP_HOST, _SMTP_PORT, timeout=30)
    smtp.ehlo()
    smtp.starttls()
    smtp.login(config.GMAIL_USER, config.GMAIL_APP_PASSWORD)
    return smtp


def _send(subject: str, html_body: str, text_body: str = "") -> bool:
    """
    Send a single email to all RECIPIENT_EMAILS.
    Returns True on success.  Retries once on failure.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.GMAIL_USER
    msg["To"] = ", ".join(config.RECIPIENT_EMAILS)

    if text_body:
        msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    for attempt in (1, 2):
        try:
            with _build_smtp() as smtp:
                smtp.sendmail(
                    config.GMAIL_USER,
                    config.RECIPIENT_EMAILS,
                    msg.as_string(),
                )
            logger.info("Email sent: %s (attempt %d)", subject, attempt)
            return True
        except smtplib.SMTPException as exc:
            logger.error("SMTP error on attempt %d: %s", attempt, exc)
            if attempt == 1:
                logger.info("Retrying in 60 seconds …")
                time.sleep(60)
    return False


# ── Digest email ──────────────────────────────────────────────────────────────

def send_digest(new_items: list, run_ts: datetime,
                watchlist: list | None = None,
                eval_stats: dict | None = None,
                summary: dict | None = None) -> bool:
    """
    Compose and send the rich HTML digest email.

    new_items: the main list — dicts from ScrapeResult.to_dict(), each carrying
               an "evaluation" key written by evaluator.py.
    watchlist: the Section 6 infosec / data-center list. Rendered separately and
               never merged into the main list.
    eval_stats: evaluator counters, used for the footer line and to say so
               plainly when scoring degraded to unscored pass-through.
    summary: the run-level closing sections from synthesis.py.
    """
    watchlist = watchlist or []
    eval_stats = eval_stats or {}
    summary = summary or {}
    if not new_items and not watchlist:
        return True  # nothing to send

    # Group by category
    by_category: dict[str, list] = {}
    for item in new_items:
        cat = item.get("category", "other") if isinstance(item, dict) else item.category
        by_category.setdefault(cat, []).append(item)

    category_counts = {cat: len(items) for cat, items in by_category.items()}
    total = len(new_items)

    if hasattr(run_ts, "strftime"):
        # %-d (Linux) vs %#d (Windows) — use lstrip("0") as a portable fallback
        day = str(run_ts.day)  # no leading zero, works everywhere
        date_str = run_ts.strftime(f"%A, %B {day}, %Y")
    else:
        date_str = str(run_ts)

    subject = f"[Opportunity] {total} match{'es' if total != 1 else ''}"
    if watchlist:
        subject += f" + {len(watchlist)} watchlist"
    subject += f" — {date_str}"

    # Jinja2 template
    env = Environment(
        loader=FileSystemLoader(str(config.TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("email_digest.html")

    # Build a portable timestamp string (%-d / %-I are Linux-only strftime codes)
    if hasattr(run_ts, "strftime"):
        hour = run_ts.hour % 12 or 12          # 12-hour, no leading zero
        ampm = "AM" if run_ts.hour < 12 else "PM"
        run_ts_str = run_ts.strftime(f"%B {run_ts.day}, %Y at {hour}:%M {ampm} UTC")
    else:
        run_ts_str = str(run_ts)

    html_body = template.render(
        total=total,
        run_ts=run_ts,
        run_ts_str=run_ts_str,
        date_str=date_str,
        by_category=by_category,
        category_counts=category_counts,
        watchlist=watchlist,
        eval_stats=eval_stats,
        summary=summary,
    )

    text_body = _plain_text_digest(new_items, total, date_str, watchlist,
                                   eval_stats, summary)

    return _send(subject, html_body, text_body)


def _render_item_text(d: dict, lines: list, watchlist_mode: bool = False) -> None:
    """Append one posting's plain-text block, mirroring the HTML card."""
    ev = d.get("evaluation") or {}

    header = d.get("title", "")
    if d.get("company") and not header.startswith(d["company"]):
        header = f"{header} - {d['company']}"
    lines.append(f"  {header}")

    # Only print a field we actually have, so this doesn't fill up with "N/A".
    facts = [d.get(k) for k in ("location", "employment_type", "seniority", "salary")]
    facts = [f for f in facts if f]
    if facts:
        lines.append(f"  {' | '.join(facts)}")
    if d.get("date"):
        lines.append(f"  Posted: {d['date']}")

    # The evaluator's annotations are the point of the digest now; the raw
    # snippet is only a fallback for an item that never got scored.
    if ev.get("why_it_fits"):
        lines.append(f"  Why it fits: {ev['why_it_fits']}")
    if ev.get("the_catch"):
        lines.append(f"  The catch: {ev['the_catch']}")
    if ev.get("ease_of_entry"):
        reason = f" ({ev['ease_reason']})" if ev.get("ease_reason") else ""
        lines.append(f"  Ease of entry: {ev['ease_of_entry']}{reason}")
    if ev.get("who_to_call"):
        lines.append(f"  Who to call: {ev['who_to_call']}")
    if watchlist_mode and ev.get("signal_read"):
        lines.append(f"  Signal: {ev['signal_read']}")
    if ev.get("unscored") and d.get("snippet"):
        lines.append(f"  (not scored this run) {d['snippet'][:200]}")

    lines.append(f"  {d.get('url', '')}")
    lines.append("")


def _plain_text_digest(items: list, total: int, date_str: str,
                       watchlist: list | None = None,
                       eval_stats: dict | None = None,
                       summary: dict | None = None) -> str:
    watchlist = watchlist or []
    eval_stats = eval_stats or {}
    summary = summary or {}

    lines = [f"Opportunity Digest - {date_str}", ""]
    if eval_stats.get("degraded"):
        lines.append("NOTE: scoring was unavailable this run. Items below are "
                     "unscored and unranked.")
        lines.append("")

    lines.append(f"MAIN LIST ({total})")
    lines.append("=" * 60)
    if not items:
        lines.append("  Nothing passed the match test this run.")
        lines.append("")
    for item in items:
        _render_item_text(item if isinstance(item, dict) else item.to_dict(), lines)

    if watchlist:
        lines.append(f"WATCHLIST - INFOSEC / DATA CENTER ({len(watchlist)})")
        lines.append("=" * 60)
        lines.append("  Market intelligence, not a shortlist.")
        lines.append("")
        if summary.get("industry_read"):
            lines.append(f"  {summary['industry_read']}")
            lines.append("")
        for item in watchlist:
            _render_item_text(item if isinstance(item, dict) else item.to_dict(),
                              lines, watchlist_mode=True)

    # Closing sections, in the order the brief asks for them.
    if summary.get("what_changed"):
        lines.append("WHAT CHANGED SINCE LAST RUN")
        lines.append("=" * 60)
        lines.append(f"  {summary['what_changed']}")
        lines.append("")

    act = summary.get("act_on")
    if act:
        lines.append("THE ONE ITEM TO ACT ON THIS WEEK")
        lines.append("=" * 60)
        label = f"  {act['title']}"
        if act.get("organization"):
            label += f" - {act['organization']}"
        lines.append(label)
        if act.get("from_watchlist"):
            lines.append("  (from the watchlist, which is unusual - see below)")
        if act.get("why"):
            lines.append(f"  Why this one: {act['why']}")
        if act.get("first_move"):
            lines.append(f"  First move: {act['first_move']}")
        if act.get("url"):
            lines.append(f"  {act['url']}")
        lines.append("")

    if summary.get("pattern_flag"):
        lines.append("PATTERN FLAG")
        lines.append("=" * 60)
        lines.append(f"  {summary['pattern_flag']}")
        lines.append("")

    if eval_stats.get("rejected"):
        lines.append(f"{eval_stats['rejected']} further postings were reviewed and "
                     f"did not meet the match test.")
    if eval_stats.get("skipped"):
        lines.append(f"{eval_stats['skipped']} postings exceeded this run's scoring "
                     f"cap and were not reviewed.")
    lines.append("")
    lines.append("Powered by Project Opportunity")
    return "\n".join(lines)


