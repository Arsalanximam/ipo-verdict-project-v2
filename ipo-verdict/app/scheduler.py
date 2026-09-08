"""
Background refresh loop. Keeps a small watchlist of company names
"warm" in the cache so requests for popular/currently-open IPOs are
fast, instead of every single request paying the full scrape cost.

This uses APScheduler's BackgroundScheduler, which runs inside the
same process as the FastAPI app -- fine for a single-instance deploy
(Render/Railway free tier). If you ever scale to multiple worker
processes, move this to a separate worker (e.g. a Celery beat task or
a platform cron job) so it doesn't run once per worker.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import REFRESH_INTERVAL_MINUTES
from app.service import refresh_company

logger = logging.getLogger(__name__)

# Add company names here that you want kept warm automatically -- e.g.
# whatever's currently open for subscription. In a fuller version this
# list could itself be scraped from an "open IPOs" listing page.
WATCHLIST = [
    "Deepa Jewellers",
    "Rays of Belief",
    "Rentomojo",
]

scheduler = BackgroundScheduler()


def _refresh_watchlist():
    for company in WATCHLIST:
        try:
            refresh_company(company)
            logger.info("Refreshed %s", company)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Scheduled refresh failed for %s: %s", company, exc)


def start_scheduler():
    scheduler.add_job(
        _refresh_watchlist,
        "interval",
        minutes=REFRESH_INTERVAL_MINUTES,
        id="refresh_watchlist",
        next_run_time=None,  # don't fire immediately on startup; first refresh happens on first request
    )
    scheduler.start()
    logger.info("Scheduler started: refreshing every %s minutes", REFRESH_INTERVAL_MINUTES)
