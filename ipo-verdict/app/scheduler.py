"""
Background refresh loop.

Automatically discovers all valid IPOs from the live GMP table and
saves their latest data into the database.
"""

from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import REFRESH_INTERVAL_MINUTES
from app.scrapers.gmp import fetch_all_gmp
from app.service import refresh_discovered_row

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()


def _refresh_market():
    """Discover and refresh all currently available IPOs."""
    try:
        rows = fetch_all_gmp()

        if not rows:
            logger.warning("Dynamic IPO discovery returned no valid rows")
            return

        refreshed = 0

        for row in rows:
            try:
                refresh_discovered_row(row)
                refreshed += 1
            except Exception as exc:
                logger.warning(
                    "Failed to save discovered IPO %r: %s",
                    row.get("matched_name"),
                    exc,
                )

        logger.info(
            "Dynamic IPO refresh complete: %d/%d rows saved",
            refreshed,
            len(rows),
        )

    except Exception as exc:
        logger.exception("Dynamic IPO discovery failed: %s", exc)


def start_scheduler():
    """Start the automatic IPO refresh scheduler."""
    if scheduler.get_job("refresh_market"):
        return

    scheduler.add_job(
        _refresh_market,
        "interval",
        minutes=REFRESH_INTERVAL_MINUTES,
        id="refresh_market",
        next_run_time=datetime.now(),
    )

    scheduler.start()

    logger.info(
        "Dynamic IPO scheduler started; refresh interval: %s minutes",
        REFRESH_INTERVAL_MINUTES,
    )