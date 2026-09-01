"""
scheduler.py — single-run launcher.

Runs the pipeline once on startup and exits. A 45-minute wall-clock timeout
aborts the run if it hangs. To re-run on a schedule, drive this from cron,
systemd, Docker's `restart` policy, or a CI scheduled trigger.
"""

import logging
import signal
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

RUN_TIMEOUT_SECONDS = 45 * 60       # 45 minutes


class RunTimeoutError(Exception):
    """Raised when the pipeline run exceeds RUN_TIMEOUT_SECONDS."""


def _timeout_handler(signum, frame):
    raise RunTimeoutError("Pipeline run exceeded 45-minute timeout.")


def run_forever(pipeline_fn) -> None:
    """
    Run pipeline_fn once with a 45-minute timeout, then return.

    Name kept for backward-compatibility with main.py's import; behaviour
    is now single-shot. Wrap this process in cron/systemd/etc. for cadence.
    """
    logger.info("Scheduler starting — single run on startup.")
    _execute_with_timeout(pipeline_fn)
    logger.info("Single-run mode: exiting after pipeline completion.")


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
        logger.warning("Pipeline run ABORTED — exceeded 45-minute timeout.")
    except Exception as exc:
        logger.error("Pipeline run ERROR: %s", exc, exc_info=True)
    finally:
        if platform.system() != "Windows":
            signal.alarm(0)  # cancel alarm
