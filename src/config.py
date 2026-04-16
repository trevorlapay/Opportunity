"""
config.py — Loads environment variables and sources.json.
All behavioral parameters are driven by env vars; no hardcoded values in logic.
"""

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
TEMPLATES_DIR = BASE_DIR / "templates"
SOURCES_FILE = DATA_DIR / "sources.json"
SEEN_ITEMS_FILE = DATA_DIR / "seen_items.json"
RUN_LOG_FILE = DATA_DIR / "run_log.json"

# ── Required credentials ───────────────────────────────────────────────────────
GMAIL_USER: str = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD: str = os.environ["GMAIL_APP_PASSWORD"]
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

_raw_recipients = os.environ.get("RECIPIENT_EMAILS", "")
RECIPIENT_EMAILS: list[str] = [e.strip() for e in _raw_recipients.split(",") if e.strip()]
if not RECIPIENT_EMAILS:
    raise ValueError("RECIPIENT_EMAILS env var is required and must not be empty")

# ── Tunable parameters ─────────────────────────────────────────────────────────
SEEN_ITEM_RETENTION_DAYS: int = int(os.getenv("SEEN_ITEM_RETENTION_DAYS", "90"))
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


def load_sources() -> list[dict]:
    """Return the list of source dicts from sources.json."""
    try:
        with open(SOURCES_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("sources", [])
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.critical("Cannot load sources.json: %s", exc)
        raise


def save_sources(sources: list[dict]) -> None:
    """Persist the source list back to sources.json."""
    with open(SOURCES_FILE, "w", encoding="utf-8") as fh:
        json.dump({"sources": sources}, fh, indent=2)
