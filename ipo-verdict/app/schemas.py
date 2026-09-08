from typing import Optional
from pydantic import BaseModel


class IPOResponse(BaseModel):
    company: str
    status: str

    price: Optional[float] = None
    ipo_size: Optional[str] = None
    lot_size: Optional[str] = None
    open_date: Optional[str] = None
    close_date: Optional[str] = None
    allotment_date: Optional[str] = None
    listing_date: Optional[str] = None
    anchor: Optional[bool] = None

    gmp_value: Optional[float] = None
    gmp_pct: Optional[float] = None
    gmp_range_low: Optional[float] = None
    gmp_range_high: Optional[float] = None
    gmp_rating_flames: Optional[int] = None
    gmp_as_of: Optional[str] = None

    sub_overall: Optional[float] = None
    sub_qib: Optional[float] = None
    sub_nii: Optional[float] = None
    sub_retail: Optional[float] = None

    star_rating: Optional[float] = None
    verdict: Optional[str] = None
    confidence: Optional[str] = None
    fetched_at: str

    class Config:
        from_attributes = True
