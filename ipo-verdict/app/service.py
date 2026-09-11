"""
The glue layer: given a company name, decide whether the cached
snapshot is fresh enough to serve, or whether to re-scrape, re-score,
and re-save. Both the API routes and the background scheduler call
into this same function.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import IPOSnapshot
from app.scrapers import nse, gmp
from app import analyzer
from app.config import CACHE_TTL_MINUTES


def _normalize(name: str) -> str:
    return name.strip().lower()


def get_cached(
    db: Session,
    company: str,
) -> Optional[IPOSnapshot]:
    return (
        db.query(IPOSnapshot)
        .filter(
            IPOSnapshot.normalized_name == _normalize(company)
        )
        .order_by(
            IPOSnapshot.fetched_at.desc()
        )
        .first()
    )


def is_fresh(snapshot: IPOSnapshot) -> bool:
    if not snapshot or not snapshot.fetched_at:
        return False

    age = (
        datetime.now(timezone.utc)
        - snapshot.fetched_at.replace(
            tzinfo=timezone.utc
        )
    )

    return age < timedelta(
        minutes=CACHE_TTL_MINUTES
    )


# ---------------------------------------------------------
# STATUS HELPERS
# ---------------------------------------------------------

def _parse_date(value) -> Optional[datetime]:
    """
    Convert common IPO date formats into a datetime.

    The scraper currently stores dates as strings, so this helper
    safely handles the formats normally returned by the GMP table.
    """

    if not value:
        return None

    if isinstance(value, datetime):
        return value

    text = str(value).strip()

    if not text:
        return None

    formats = [
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d-%b-%Y",
        "%d %b %Y",
        "%d %B %Y",
        "%Y-%m-%d",
        "%Y/%m/%d",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(
                text,
                fmt,
            )
        except ValueError:
            continue

    return None


def calculate_status(
    source_status: Optional[str],
    open_date=None,
    close_date=None,
    allotment_date=None,
    listing_date=None,
) -> str:
    """
    Calculate a clean IPO lifecycle status.

    Priority:

    1. Listed       -> listing date has arrived
    2. Allotted     -> allotment date has arrived
    3. Closed       -> issue closed but allotment/listing not reached
    4. Open         -> issue is currently open
    5. Upcoming     -> issue has not opened yet
    6. Fallback     -> use a valid source status
    """

    now = datetime.now()

    opened = _parse_date(open_date)
    closed = _parse_date(close_date)
    allotted = _parse_date(allotment_date)
    listed = _parse_date(listing_date)

    # Listing date is the strongest lifecycle signal.
    if listed and now >= listed:
        return "listed"

    # Allotment has happened but listing has not happened yet.
    if allotted and now >= allotted:
        return "allotted"

    # Issue has closed.
    if closed and now > closed:
        return "closed"

    # Issue is currently open.
    if opened and closed:
        if opened <= now <= closed:
            return "open"

    elif opened:
        if now >= opened:
            return "open"

    # Issue has not opened yet.
    if opened and now < opened:
        return "upcoming"

    # Safe fallback to the scraper status.
    valid_statuses = {
        "upcoming",
        "open",
        "closed",
        "allotted",
        "listed",
    }

    if source_status:
        clean_source_status = (
            str(source_status)
            .strip()
            .lower()
        )

        if clean_source_status in valid_statuses:
            return clean_source_status

    return "unknown"


def get_or_refresh(
    company: str,
    force: bool = False,
) -> IPOSnapshot:

    db = SessionLocal()

    try:
        cached = get_cached(
            db,
            company,
        )

        if (
            cached
            and is_fresh(cached)
            and not force
        ):
            return cached

        return _refresh(
            db,
            company,
        )

    finally:
        db.close()


def refresh_company(
    company: str,
) -> IPOSnapshot:

    db = SessionLocal()

    try:
        return _refresh(
            db,
            company,
        )

    finally:
        db.close()


def _refresh(
    db: Session,
    company: str,
) -> IPOSnapshot:

    g = (
        gmp.fetch_gmp(company)
        or {}
    )

    sub_nse = (
        nse.fetch_subscription(company)
        or {}
    )

    status = calculate_status(
        source_status=g.get("status"),
        open_date=g.get("open_date"),
        close_date=g.get("close_date"),
        allotment_date=g.get("allotment_date"),
        listing_date=g.get("listing_date"),
    )

    scores = {
        "subscription": analyzer.score_subscription(
            g.get("subscription_overall")
            or sub_nse.get("overall"),
            sub_nse.get("qib"),
        ),

        "financials": None,

        "valuation": None,

        "anchor": analyzer.score_anchor_flag(
            g.get("anchor")
            if g
            else None
        ),

        "gmp": analyzer.score_gmp(
            g.get("value"),
            issue_price=g.get("price"),
        ),
    }

    stars = analyzer.composite_rating(
        scores
    )

    facts = {
        "gmp": g,
        "nse_subscription": sub_nse,
    }

    verdict = (
        analyzer.llm_verdict(
            company,
            facts,
            stars,
        )
        or analyzer.template_verdict(
            scores,
            stars,
        )
    )

    found_anything = (
        bool(g)
        or bool(sub_nse)
    )

    snapshot = IPOSnapshot(
        company=company,

        normalized_name=_normalize(
            company
        ),

        status=status,

        price=g.get("price"),

        ipo_size=g.get(
            "ipo_size"
        ),

        lot_size=g.get(
            "lot_size"
        ),

        open_date=g.get(
            "open_date"
        ),

        close_date=g.get(
            "close_date"
        ),

        allotment_date=g.get(
            "allotment_date"
        ),

        listing_date=g.get(
            "listing_date"
        ),

        anchor=g.get(
            "anchor"
        ),

        gmp_value=g.get(
            "value"
        ),

        gmp_pct=g.get(
            "pct"
        ),

        gmp_range_low=g.get(
            "range_low"
        ),

        gmp_range_high=g.get(
            "range_high"
        ),

        gmp_rating_flames=g.get(
            "rating_flames"
        ),

        gmp_as_of=g.get(
            "as_of"
        ),

        sub_overall=g.get(
            "subscription_overall"
        ),

        sub_qib=sub_nse.get(
            "qib"
        ),

        sub_nii=sub_nse.get(
            "nii"
        ),

        sub_retail=sub_nse.get(
            "retail"
        ),

        star_rating=stars,

        verdict=verdict,

        confidence=(
            "high"
            if (
                g
                and g.get("value")
                and g.get(
                    "subscription_overall"
                )
            )
            else "medium"
            if found_anything
            else "low"
        ),
    )

    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)

    return snapshot


def refresh_discovered_row(
    g: dict,
) -> IPOSnapshot:
    """
    Persist one already-parsed row from the bulk GMP discovery scrape.

    Unlike refresh_company(), this does not launch another browser.
    The scheduler already fetched and validated the live row, so this
    function only scores it and writes a snapshot.
    """

    db = SessionLocal()

    try:
        company = (
            g.get("matched_name")
            or ""
        ).strip()

        if not company:
            raise ValueError(
                "Discovered GMP row has no company name"
            )

        # Fetch category-wise subscription data from NSE for scheduler refreshes.
        # The bulk GMP discovery already provides the IPO row, but it does not
        # contain reliable QIB/NII/Retail values.
        sub_nse = nse.fetch_subscription(company) or {}

        status = calculate_status(
            source_status=g.get("status"),
            open_date=g.get(
                "open_date"
            ),
            close_date=g.get(
                "close_date"
            ),
            allotment_date=g.get(
                "allotment_date"
            ),
            listing_date=g.get(
                "listing_date"
            ),
        )

        subscription_overall = (
            sub_nse.get("overall")
            if sub_nse.get("overall") is not None
            else g.get("subscription_overall")
        )

        scores = {
            "subscription": analyzer.score_subscription(
                subscription_overall,
                sub_nse.get("qib"),
            ),

            "financials": None,

            "valuation": None,

            "anchor": analyzer.score_anchor_flag(
                g.get("anchor")
            ),

            "gmp": analyzer.score_gmp(
                g.get("value"),
                issue_price=g.get(
                    "price"
                ),
            ),
        }

        stars = analyzer.composite_rating(
            scores
        )

        facts = {
            "gmp": g,
            "nse_subscription": sub_nse,
        }

        verdict = (
            analyzer.llm_verdict(
                company,
                facts,
                stars,
            )
            or analyzer.template_verdict(
                scores,
                stars,
            )
        )

        snapshot = IPOSnapshot(
            company=company,

            normalized_name=_normalize(
                company
            ),

            status=status,

            price=g.get(
                "price"
            ),

            ipo_size=g.get(
                "ipo_size"
            ),

            lot_size=g.get(
                "lot_size"
            ),

            open_date=g.get(
                "open_date"
            ),

            close_date=g.get(
                "close_date"
            ),

            allotment_date=g.get(
                "allotment_date"
            ),

            listing_date=g.get(
                "listing_date"
            ),

            anchor=g.get(
                "anchor"
            ),

            gmp_value=g.get(
                "value"
            ),

            gmp_pct=g.get(
                "pct"
            ),

            gmp_range_low=g.get(
                "range_low"
            ),

            gmp_range_high=g.get(
                "range_high"
            ),

            gmp_rating_flames=g.get(
                "rating_flames"
            ),

            gmp_as_of=g.get(
                "as_of"
            ),

            sub_overall=subscription_overall,

            sub_qib=sub_nse.get(
                "qib"
            ),

            sub_nii=sub_nse.get(
                "nii"
            ),

            sub_retail=sub_nse.get(
                "retail"
            ),

            star_rating=stars,

            verdict=verdict,

            confidence=(
                "high"
                if (
                    g.get("value")
                    is not None
                    and g.get(
                        "subscription_overall"
                    )
                    is not None
                )
                else "medium"
            ),
        )

        db.add(snapshot)
        db.commit()
        db.refresh(snapshot)

        return snapshot

    finally:
        db.close()