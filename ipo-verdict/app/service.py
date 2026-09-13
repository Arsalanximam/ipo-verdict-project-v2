"""IPO data service layer.

Keeps the existing GMP workflow, but makes NSE subscription data a real
secondary source instead of allowing a failed NSE request to wipe historical
subscription categories.  Discovered bulk GMP rows also receive NSE
subscription data before being persisted.
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
    return str(name or "").strip().lower()


def get_cached(db: Session, company: str) -> Optional[IPOSnapshot]:
    return (
        db.query(IPOSnapshot)
        .filter(IPOSnapshot.normalized_name == _normalize(company))
        .order_by(IPOSnapshot.fetched_at.desc())
        .first()
    )


def is_fresh(snapshot: IPOSnapshot) -> bool:
    if not snapshot or not snapshot.fetched_at:
        return False
    fetched = snapshot.fetched_at
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - fetched
    return age < timedelta(minutes=CACHE_TTL_MINUTES)


def _latest_existing(db: Session, company: str) -> Optional[IPOSnapshot]:
    return (
        db.query(IPOSnapshot)
        .filter(IPOSnapshot.normalized_name == _normalize(company))
        .order_by(IPOSnapshot.fetched_at.desc())
        .first()
    )


def _merge_subscription(previous: Optional[IPOSnapshot], nse_data: dict, g: dict) -> dict:
    """Choose the freshest available subscription value without data loss."""
    nse_data = nse_data or {}

    overall = nse_data.get("overall")
    if overall is None:
        overall = g.get("subscription_overall")
    if overall is None and previous is not None:
        overall = previous.sub_overall

    qib = nse_data.get("qib")
    nii = nse_data.get("nii")
    retail = nse_data.get("retail")

    if qib is None and previous is not None:
        qib = previous.sub_qib
    if nii is None and previous is not None:
        nii = previous.sub_nii
    if retail is None and previous is not None:
        retail = previous.sub_retail

    return {
        "overall": overall,
        "qib": qib,
        "nii": nii,
        "retail": retail,
    }


def get_or_refresh(company: str, force: bool = False) -> IPOSnapshot:
    """
    Serve the latest persisted snapshot without scraping during an API request.

    Market ingestion is handled by the scheduler through refresh_company() /
    refresh_discovered_row(). Keeping external scraping out of the request path
    prevents slow NSE/GMP sources and Playwright startup from blocking users.
    """
    db = SessionLocal()
    try:
        cached = get_cached(db, company)

        # API requests are read-only with respect to external sources.
        # Even when the snapshot is stale, return the last known value quickly.
        # The background scheduler is responsible for refreshing it.
        if cached:
            return cached

        return None
    finally:
        db.close()


def refresh_company(company: str) -> IPOSnapshot:
    db = SessionLocal()
    try:
        return _refresh(db, company)
    finally:
        db.close()


def _build_snapshot(db: Session, company: str, g: dict, sub_nse: dict) -> IPOSnapshot:
    previous = _latest_existing(db, company)
    subscription = _merge_subscription(previous, sub_nse, g)

    scores = {
        "subscription": analyzer.score_subscription(
            subscription["overall"],
            subscription["qib"],
        ),
        "financials": None,
        "valuation": None,
        "anchor": analyzer.score_anchor_flag(g.get("anchor") if g else None),
        "gmp": analyzer.score_gmp(g.get("value"), issue_price=g.get("price")),
    }
    stars = analyzer.composite_rating(scores)

    facts = {
        "gmp": g,
        "nse_subscription": subscription,
    }
    verdict = analyzer.llm_verdict(company, facts, stars) or analyzer.template_verdict(scores, stars)

    found_anything = bool(g) or bool(sub_nse) or bool(previous)

    snapshot = IPOSnapshot(
        company=company,
        normalized_name=_normalize(company),
        status=g.get("status", "unknown") if g else (previous.status if previous else "unknown"),
        price=g.get("price") if g.get("price") is not None else (previous.price if previous else None),
        ipo_size=g.get("ipo_size") if g.get("ipo_size") is not None else (previous.ipo_size if previous else None),
        lot_size=g.get("lot_size") if g.get("lot_size") is not None else (previous.lot_size if previous else None),
        open_date=g.get("open_date") if g.get("open_date") is not None else (previous.open_date if previous else None),
        close_date=g.get("close_date") if g.get("close_date") is not None else (previous.close_date if previous else None),
        allotment_date=g.get("allotment_date") if g.get("allotment_date") is not None else (previous.allotment_date if previous else None),
        listing_date=g.get("listing_date") if g.get("listing_date") is not None else (previous.listing_date if previous else None),
        anchor=g.get("anchor") if g.get("anchor") is not None else (previous.anchor if previous else None),
        gmp_value=g.get("value") if g.get("value") is not None else (previous.gmp_value if previous else None),
        gmp_pct=g.get("pct") if g.get("pct") is not None else (previous.gmp_pct if previous else None),
        gmp_range_low=g.get("range_low") if g.get("range_low") is not None else (previous.gmp_range_low if previous else None),
        gmp_range_high=g.get("range_high") if g.get("range_high") is not None else (previous.gmp_range_high if previous else None),
        gmp_rating_flames=g.get("rating_flames") if g.get("rating_flames") is not None else (previous.gmp_rating_flames if previous else None),
        gmp_as_of=g.get("as_of") if g.get("as_of") is not None else (previous.gmp_as_of if previous else None),
        sub_overall=subscription["overall"],
        sub_qib=subscription["qib"],
        sub_nii=subscription["nii"],
        sub_retail=subscription["retail"],
        star_rating=stars,
        verdict=verdict,
        confidence=(
            "high"
            if (g.get("value") is not None and subscription["overall"] is not None)
            else "medium"
            if found_anything
            else "low"
        ),
    )
    return snapshot


def _refresh(db: Session, company: str) -> IPOSnapshot:
    g = gmp.fetch_gmp(company) or {}
    sub_nse = nse.fetch_subscription(company) or {}

    snapshot = _build_snapshot(db, company, g, sub_nse)
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot


def refresh_discovered_row(g: dict) -> IPOSnapshot:
    """Persist a bulk-discovered GMP row plus NSE subscription data.

    The scheduler already has the live GMP row, so this function does not
    launch another GMP browser. NSE subscription data is fetched through the
    cached NSE scraper. Existing category values are preserved if NSE is
    temporarily unavailable.
    """
    db = SessionLocal()
    try:
        company = (g.get("matched_name") or g.get("company") or "").strip()
        if not company:
            raise ValueError("Discovered GMP row has no company name")

        sub_nse = nse.fetch_subscription(company) or {}
        snapshot = _build_snapshot(db, company, g, sub_nse)
        db.add(snapshot)
        db.commit()
        db.refresh(snapshot)
        return snapshot
    finally:
        db.close()
