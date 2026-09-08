"""
FastAPI entrypoint.

Run locally:
    uvicorn app.main:app --reload

Then open http://127.0.0.1:8000/ for the dashboard, or
http://127.0.0.1:8000/docs for the auto-generated Swagger UI.
"""
import logging

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.database import init_db, SessionLocal
from app.models import IPOSnapshot
from app.schemas import IPOResponse
from app.service import get_or_refresh
from app.scheduler import start_scheduler
from app.config import CORS_ORIGINS

logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="IPO Verdict API",
    description="Live-scraped GMP, dates, and subscription data, reduced to one star rating.",
    version="1.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    init_db()
    start_scheduler()


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/ipo/{company}", response_model=IPOResponse)
def get_ipo(company: str, force: bool = False):
    if not company.strip():
        raise HTTPException(400, "Company name can't be empty.")
    s = get_or_refresh(company, force=force)
    return IPOResponse(
        company=s.company,
        status=s.status,
        price=s.price,
        ipo_size=s.ipo_size,
        lot_size=s.lot_size,
        open_date=s.open_date,
        close_date=s.close_date,
        allotment_date=s.allotment_date,
        listing_date=s.listing_date,
        anchor=s.anchor,
        gmp_value=s.gmp_value,
        gmp_pct=s.gmp_pct,
        gmp_range_low=s.gmp_range_low,
        gmp_range_high=s.gmp_range_high,
        gmp_rating_flames=s.gmp_rating_flames,
        gmp_as_of=s.gmp_as_of,
        sub_overall=s.sub_overall,
        sub_qib=s.sub_qib,
        sub_nii=s.sub_nii,
        sub_retail=s.sub_retail,
        star_rating=s.star_rating,
        verdict=s.verdict,
        confidence=s.confidence,
        fetched_at=s.fetched_at.isoformat(),
    )


@app.get("/api/ipos")
def list_tracked():
    db = SessionLocal()
    try:
        rows = (
            db.query(IPOSnapshot)
            .order_by(IPOSnapshot.fetched_at.desc())
            .limit(50)
            .all()
        )
        seen = set()
        out = []
        for r in rows:
            if r.normalized_name in seen:
                continue
            seen.add(r.normalized_name)
            out.append({
                "company": r.company,
                "star_rating": r.star_rating,
                "status": r.status,
                "fetched_at": r.fetched_at.isoformat(),
            })
        return out
    finally:
        db.close()


app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/")
def dashboard():
    return FileResponse("frontend/index.html")
