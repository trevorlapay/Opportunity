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

def send_digest(new_items: list, run_ts: datetime) -> bool:
    """
    Compose and send the rich HTML digest email.
    new_items: list of ScrapeResult-like dicts (from ScrapeResult.to_dict()).
    """
    if not new_items:
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

    subject = f"[Opportunity] 🗂 {total} new finding{'s' if total != 1 else ''} — {date_str}"

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
    )

    text_body = _plain_text_digest(new_items, total, date_str)

    return _send(subject, html_body, text_body)


def _plain_text_digest(items: list, total: int, date_str: str) -> str:
    lines = [f"Opportunity Digest — {date_str}", f"{total} new item(s) found.\n"]
    for item in items:
        d = item if isinstance(item, dict) else item.to_dict()
        lines.append(f"  [{d.get('category','').upper()}] {d.get('title','')}")
        lines.append(f"  Location: {d.get('location', 'N/A')}")
        lines.append(f"  URL: {d.get('url', '')}")
        lines.append("")
    lines.append("—\nPowered by Project Opportunity")
    return "\n".join(lines)


