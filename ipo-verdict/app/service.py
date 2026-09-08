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
    age = datetime.now(timezone.utc) - snapshot.fetched_at.replace(tzinfo=timezone.utc)
    return age < timedelta(minutes=CACHE_TTL_MINUTES)


def get_or_refresh(company: str, force: bool = False) -> IPOSnapshot:
    db = SessionLocal()
    try:
        cached = get_cached(db, company)
        if cached and is_fresh(cached) and not force:
            return cached
        return _refresh(db, company)
    finally:
        db.close()


def refresh_company(company: str) -> IPOSnapshot:
    db = SessionLocal()
    try:
        return _refresh(db, company)
    finally:
        db.close()


def _refresh(db: Session, company: str) -> IPOSnapshot:
    g = gmp.fetch_gmp(company) or {}
    sub_nse = nse.fetch_subscription(company) or {}  # kept as a secondary source; often empty (see README)

    scores = {
        "subscription": analyzer.score_subscription(
            g.get("subscription_overall") or sub_nse.get("overall"),
            sub_nse.get("qib"),
        ),
        "financials": None,   # not scraped in this version -- see README "next features"
        "valuation": None,    # not scraped in this version -- see README "next features"
        "anchor": analyzer.score_anchor_flag(g.get("anchor") if g else None),
        "gmp": analyzer.score_gmp(g.get("value"), issue_price=g.get("price")),
    }
    stars = analyzer.composite_rating(scores)

    facts = {"gmp": g, "nse_subscription": sub_nse}
    verdict = analyzer.llm_verdict(company, facts, stars) or analyzer.template_verdict(scores, stars)

    found_anything = bool(g) or bool(sub_nse)
    snapshot = IPOSnapshot(
        company=company,
        normalized_name=_normalize(company),
        status=g.get("status", "unknown") if g else ("unknown" if found_anything else "not_found"),
        price=g.get("price"),
        ipo_size=g.get("ipo_size"),
        lot_size=g.get("lot_size"),
        open_date=g.get("open_date"),
        close_date=g.get("close_date"),
        allotment_date=g.get("allotment_date"),
        listing_date=g.get("listing_date"),
        anchor=g.get("anchor"),
        gmp_value=g.get("value"),
        gmp_pct=g.get("pct"),
        gmp_range_low=g.get("range_low"),
        gmp_range_high=g.get("range_high"),
        gmp_rating_flames=g.get("rating_flames"),
        gmp_as_of=g.get("as_of"),
        sub_overall=g.get("subscription_overall"),
        sub_qib=sub_nse.get("qib"),
        sub_nii=sub_nse.get("nii"),
        sub_retail=sub_nse.get("retail"),
        star_rating=stars,
        verdict=verdict,
        confidence="high" if (g and g.get("value") and g.get("subscription_overall")) else "medium" if found_anything else "low",
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot
