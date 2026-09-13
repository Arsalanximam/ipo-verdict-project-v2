"""
One-shot market ingestion worker.

This worker is intentionally separate from the FastAPI web process.
It performs one complete live GMP discovery + NSE subscription refresh,
persists the results to PostgreSQL, and then exits.

Render Cron can run this command on a schedule:
    python -m app.worker
"""

import logging

from app.database import init_db
from app.scrapers import gmp
from app.service import refresh_discovered_row


logging.basicConfig(
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


def run_once() -> tuple[int, int]:
    """Run one complete market refresh and return (saved, failed)."""
    init_db()

    logger.info("Starting one-shot IPO market refresh")

    rows = gmp.fetch_all_gmp()

    if not rows:
        logger.warning("No valid IPO rows discovered from GMP tracker")
        return 0, 0

    saved = 0
    failed = 0

    for row in rows:
        company = str(
            row.get("matched_name")
            or row.get("company")
            or ""
        ).strip()

        try:
            refresh_discovered_row(row)
            saved += 1
            logger.info("Saved IPO snapshot: %s", company or "<unknown>")
        except Exception:
            failed += 1
            logger.exception(
                "Failed to refresh IPO snapshot: %s",
                company or "<unknown>",
            )

    logger.info(
        "One-shot IPO refresh complete: %d/%d rows saved, %d failed",
        saved,
        len(rows),
        failed,
    )

    return saved, failed


if __name__ == "__main__":
    run_once()
