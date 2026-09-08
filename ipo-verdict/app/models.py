from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text

from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class IPOSnapshot(Base):
    __tablename__ = "ipo_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    company = Column(String, index=True, nullable=False)
    normalized_name = Column(String, index=True, nullable=False)

    status = Column(String, default="unknown")

    price = Column(Float, nullable=True)
    ipo_size = Column(String, nullable=True)
    lot_size = Column(String, nullable=True)
    open_date = Column(String, nullable=True)
    close_date = Column(String, nullable=True)
    allotment_date = Column(String, nullable=True)
    listing_date = Column(String, nullable=True)
    anchor = Column(Boolean, nullable=True)

    gmp_value = Column(Float, nullable=True)
    gmp_pct = Column(Float, nullable=True)
    gmp_range_low = Column(Float, nullable=True)
    gmp_range_high = Column(Float, nullable=True)
    gmp_rating_flames = Column(Integer, nullable=True)
    gmp_as_of = Column(String, nullable=True)

    sub_overall = Column(Float, nullable=True)  # from investorgain's SUB column
    sub_qib = Column(Float, nullable=True)       # from NSE, when available
    sub_nii = Column(Float, nullable=True)
    sub_retail = Column(Float, nullable=True)

    star_rating = Column(Float, nullable=True)
    verdict = Column(Text, nullable=True)
    confidence = Column(String, nullable=True)

    fetched_at = Column(DateTime, default=utcnow)
