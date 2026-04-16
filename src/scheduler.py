"""
scheduler.py — 6-hour cron-style scheduler.

Runs the pipeline immediately on startup, then once every 6 hours.
A 45-minute wall-clock timeout aborts any run that runs too long.
"""

import logging
import signal
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

RUN_INTERVAL_SECONDS = 6 * 3600     # 6 hours
RUN_TIMEOUT_SECONDS  = 45 * 60      # 45 minutes


class RunTimeoutError(Exception):
    """Raised when a single pipeline run exceeds RUN_TIMEOUT_SECONDS."""


def _timeout_handler(signum, frame):
    raise RunTimeoutError("Pipeline run exceeded 45-minute timeout.")


def run_forever(pipeline_fn) -> None:
    """
    Execute pipeline_fn immediately, then once every 6 hours.
    Catches all exceptions so the scheduler never dies.
    """
    logger.info("Scheduler starting — running pipeline immediately on startup.")

    # First run: immediate, no delay
    _execute_with_timeout(pipeline_fn)

    while True:
        logger.info("Next run in 6 hours.")
        time.sleep(RUN_INTERVAL_SECONDS)
        _execute_with_timeout(pipeline_fn)


def _execute_with_timeout(pipeline_fn) -> None:
    """Run pipeline_fn with a 45-minute hard timeout (SIGALRM, Unix only)."""
    import platform

    if platform.system() != "Windows":
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(RUN_TIMEOUT_SECONDS)

    try:
        logger.info("=" * 60)
        logger.info("Pipeline run starting at %s", datetime.now(timezone.utc).isoformat())
        pipeline_fn()
        logger.info("Pipeline run completed.")
    except RunTimeoutError:
        logger.warning("Pipeline run ABORTED — exceeded 45-minute timeout. Will resume next hour.")
    except Exception as exc:
        logger.error("Pipeline run ERROR: %s", exc, exc_info=True)
    finally:
        if platform.system() != "Windows":
            signal.alarm(0)  # cancel alarm
