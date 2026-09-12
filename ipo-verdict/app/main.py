import logging
import re
from html.parser import HTMLParser
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from datetime import datetime, date, timedelta
from statistics import median

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


# ---------------------------------------------------------
# APP
# ---------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="IPO Verdict",
    version="1.0.0",
)


# ---------------------------------------------------------
# CORS
# ---------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------
# STARTUP
# ---------------------------------------------------------

@app.on_event("startup")
def startup_event():
    init_db()
    start_scheduler()


# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------

def parse_ipo_date(value):
    if not value:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    text = str(value).strip()

    formats = [
        "%d-%b-%Y",
        "%d-%B-%Y",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y-%m-%d",
        "%d-%b",
        "%d-%B",
    ]

    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt).date()
            if "%Y" not in fmt:
                parsed = parsed.replace(year=datetime.now().year)
            return parsed
        except ValueError:
            continue

    return None


def is_closing_soon(
    open_date,
    close_date,
    listing_date=None,
    window_days=1,
):
    today = date.today()
    opening = parse_ipo_date(open_date)
    closing = parse_ipo_date(close_date)
    listing = parse_ipo_date(listing_date)

    if listing and today >= listing:
        return False

    if not opening or not closing:
        return False

    if not (opening <= today <= closing):
        return False

    return 0 <= (closing - today).days <= window_days


def calculate_status(
    open_date,
    close_date,
    listing_date,
    current_status=None,
):
    today = date.today()

    listing = parse_ipo_date(listing_date)
    opening = parse_ipo_date(open_date)
    closing = parse_ipo_date(close_date)

    if listing and today >= listing:
        return "listed"

    if closing and today > closing:
        return "closed"

    if opening and closing:
        if opening <= today <= closing:
            return "open"

    if opening and today < opening:
        return "upcoming"

    if current_status:
        return str(current_status).lower()

    return "unknown"




def is_valid_ipo_snapshot(row):
    """Return True when a snapshot contains enough data to represent an IPO.

    A newer scraper row can occasionally be incomplete. We should not let that
    incomplete row replace an older, complete snapshot for the same IPO.
    """
    if row.price is None:
        return False

    try:
        if float(row.price) <= 0:
            return False
    except (TypeError, ValueError):
        return False

    has_date = any(
        getattr(row, field, None)
        for field in (
            "open_date",
            "close_date",
            "allotment_date",
            "listing_date",
        )
    )

    has_size = bool(getattr(row, "ipo_size", None))
    has_lot = bool(getattr(row, "lot_size", None))

    # Reject legacy corrupted snapshots where the scraper accidentally stored
    # the IPO size (for example "₹351.03 Cr") as the company name.
    company_text = str(getattr(row, "company", "") or "").strip().lower()
    size_text = str(getattr(row, "ipo_size", "") or "").strip().lower()
    if company_text and size_text and company_text == size_text:
        return False

    return bool(has_date and (has_size or has_lot))


def latest_valid_rows(db, max_rows=None):
    """Return one complete/latest snapshot per IPO name.

    Rows are scanned newest-first. If the newest row for an IPO is incomplete,
    an older complete snapshot is used instead. This prevents autocomplete and
    dashboard cards from showing values such as status=unknown or missing price.
    """
    query = (
        db.query(IPOSnapshot)
        .order_by(IPOSnapshot.fetched_at.desc())
    )

    if max_rows is not None:
        query = query.limit(max_rows)

    rows = query.all()

    latest_by_name = {}

    for row in rows:
        company = str(row.company or "").strip()
        normalized = str(
            row.normalized_name or company
        ).strip().lower()

        if not company or not normalized:
            continue

        if normalized in latest_by_name:
            continue

        if not is_valid_ipo_snapshot(row):
            continue

        latest_by_name[normalized] = row

    return list(latest_by_name.values())


def estimated_listing_price(price, gmp):
    if price is None:
        return None

    if gmp is None:
        return price

    return round(
        float(price) + float(gmp),
        2,
    )


def estimated_listing_premium(price, gmp):
    if price is None or price == 0 or gmp is None:
        return None

    return round(
        (float(gmp) / float(price)) * 100,
        2,
    )


def lot_investment(price, lot_size):
    if price is None or not lot_size:
        return None

    try:
        number = int(
            str(lot_size)
            .replace(",", "")
            .strip()
        )

        return round(
            float(price) * number,
            2,
        )

    except (
        ValueError,
        TypeError,
    ):
        return None


def normalize_subscription_category(rows, field_name, latest_value):
    """Preserve real category values while treating scraper-only zeroes as unavailable.

    Some scraper paths write 0 when QIB/NII/Retail was not returned. We can
    distinguish that case when the entire historical series for that category
    is zero/empty. If any historical snapshot contains a positive value, a
    latest 0 is preserved as a real zero.
    """
    if latest_value is None:
        return None

    try:
        latest_number = float(latest_value)
    except (TypeError, ValueError):
        return latest_value

    if latest_number != 0:
        return latest_value

    historical_values = []
    for item in rows:
        value = getattr(item, field_name, None)
        if value is None:
            continue
        try:
            historical_values.append(float(value))
        except (TypeError, ValueError):
            continue

    if any(value > 0 for value in historical_values):
        return latest_value

    return None

# ---------------------------------------------------------
# FINANCIAL + VALUATION INTELLIGENCE
# ---------------------------------------------------------

class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.tables=[]; self.table=None; self.row=None; self.cell=None
    def handle_starttag(self, tag, attrs):
        tag=tag.lower()
        if tag=="table": self.table=[]
        elif tag=="tr" and self.table is not None: self.row=[]
        elif tag in ("td","th") and self.row is not None: self.cell=[]
    def handle_data(self, data):
        if self.cell is not None: self.cell.append(data)
    def handle_endtag(self, tag):
        tag=tag.lower()
        if tag in ("td","th") and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split())); self.cell=None
        elif tag=="tr" and self.row is not None:
            if self.row: self.table.append(self.row)
            self.row=None
        elif tag=="table" and self.table is not None:
            if self.table: self.tables.append(self.table)
            self.table=None

def _fin_slug(company):
    return re.sub(r"[^a-z0-9]+","-",str(company).lower()).strip("-")+"-ipo-2026"

def _num(value):
    if value is None: return None
    m=re.search(r"-?\d+(?:,\d{3})*(?:\.\d+)?",str(value))
    return float(m.group(0).replace(",","")) if m else None

def _page_text(html):
    class P(HTMLParser):
        def __init__(self): super().__init__(); self.out=[]
        def handle_data(self,d): self.out.append(d)
    p=P(); p.feed(html); return " ".join(" ".join(p.out).split())

def _financial_rows(html):
    p=_TableParser(); p.feed(html)
    for table in p.tables:
        hi=None
        for i,row in enumerate(table):
            s=" ".join(row).lower()
            if "revenue" in s and "pat" in s and "ebitda" in s:
                hi=i; break
        if hi is None: continue
        heads=[re.sub(r"[^a-z0-9]+"," ",x.lower()).strip() for x in table[hi]]
        out=[]
        for row in table[hi+1:]:
            if not row or not re.search(r"fy\s*20\d{2}",row[0],re.I): continue
            vals={heads[i]:_num(v) for i,v in enumerate(row) if i<len(heads)}
            out.append({"period":row[0],"revenue":vals.get("revenue"),"pat":vals.get("pat"),"ebitda":vals.get("ebitda"),"net_worth":vals.get("net worth"),"total_assets":vals.get("total assets"),"borrowings":vals.get("borrowings"),"eps":vals.get("eps")})
        if out: return out
    return []

def _metric(text, pattern):
    m=re.search(pattern,text,re.I); return _num(m.group(1)) if m else None


def _peer_median_pe(text):
    patterns = [
        r"median\s+peer\s+P/E[^.]{0,160}?(?:about|around|approximately|of)\s*([\d,.]+)\s*[x×]",
        r"median\s+peer\s+P/E[^.]{0,160}?([\d,.]+)\s*[x×]",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            try:
                value = float(m.group(1).replace(',', ''))
                if value >= 5:
                    return value
            except ValueError:
                pass
    return None

def _valuation(text):
    return {
      "pe_pre_issue":_metric(text,r"P/E\s*\(pre-issue\)\s*[: ]+([\d.]+)x?"),
      "pe_post_issue":_metric(text,r"P/E\s*\(post-issue\)\s*[: ]+([\d.]+)x?"),
      "ro_nw":_metric(text,r"RoNW\s*[: ]+([\d.]+)%"),
      "ro_e":_metric(text,r"RoE\s*[: ]+([\d.]+)%"),
      "ro_ce":_metric(text,r"RoCE\s*[: ]+([\d.]+)%"),
      "debt_equity":_metric(text,r"Debt\s*/\s*Equity\s*[: ]+([\d.]+)"),
      "eps_pre_issue":_metric(text,r"EPS\s*\(pre-issue\)\s*[: ]+₹?\s*([\d.]+)"),
      "eps_post_issue":_metric(text,r"EPS\s*\(post-issue\)\s*[: ]+₹?\s*([\d.]+)"),
      "ebitda_margin":_metric(text,r"EBITDA\s+Margin\s*[: ]+([\d.]+)%"),
      "pat_margin":_metric(text,r"PAT\s+Margin\s*[: ]+([\d.]+)%"),
      "pbv":_metric(text,r"P/BV\s*[: ]+([\d.]+)x?"),
      "nav_per_share":_metric(text,r"NAV\s*/\s*share\s*[: ]+₹?\s*([\d.]+)"),
      "market_cap":_metric(text,r"Market\s*Cap\s*[: ]+₹?\s*([\d,.]+)"),
      "peer_median_pe": _peer_median_pe(text)
    }

def _growth(a,b): return round((b-a)/abs(a)*100,2) if a is not None and b is not None and a!=0 else None

def _financial_analysis(rows,v):
    latest=rows[-1] if rows else {}; prev=rows[-2] if len(rows)>1 else {}
    rg=_growth(prev.get("revenue"),latest.get("revenue")); pg=_growth(prev.get("pat"),latest.get("pat"))
    pos=[]; risks=[]
    if rg is not None: (pos if rg>=0 else risks).append(f"Revenue {'grew' if rg>=0 else 'fell'} {abs(rg):.1f}% in the latest reported year.")
    if pg is not None: (pos if pg>=0 else risks).append(f"PAT {'grew' if pg>=0 else 'fell'} {abs(pg):.1f}% in the latest reported year.")
    if v.get("ro_e") is not None and v["ro_e"]>=20: pos.append(f"ROE is {v['ro_e']:.1f}%.")
    if v.get("ro_e") is not None and v["ro_e"]<10: risks.append(f"ROE is only {v['ro_e']:.1f}%.")
    if v.get("debt_equity") is not None and v["debt_equity"]<=.5: pos.append(f"Debt/equity is low at {v['debt_equity']:.2f}.")
    if v.get("debt_equity") is not None and v["debt_equity"]>1: risks.append(f"Debt/equity is elevated at {v['debt_equity']:.2f}.")
    if v.get("pe_post_issue") is not None and v.get("peer_median_pe") is not None:
        if v["pe_post_issue"]<=v["peer_median_pe"]: pos.append(f"Post-issue P/E {v['pe_post_issue']:.2f}x is below disclosed peer median {v['peer_median_pe']:.1f}x.")
        else: risks.append(f"Post-issue P/E {v['pe_post_issue']:.2f}x is above disclosed peer median {v['peer_median_pe']:.1f}x.")
    checks=[rg is None or rg>=0,pg is None or pg>=0,v.get("ro_e") is None or v["ro_e"]>=15,v.get("debt_equity") is None or v["debt_equity"]<=1,v.get("pe_post_issue") is None or v.get("peer_median_pe") is None or v["pe_post_issue"]<=v["peer_median_pe"]]
    known=sum(x is not None for x in [rg,pg,v.get("ro_e"),v.get("debt_equity"),v.get("pe_post_issue") if v.get("peer_median_pe") is not None else None])
    score=round(sum(checks)/len(checks)*100) if known else None
    label="Strong fundamentals" if score is not None and score>=80 else "Positive fundamentals" if score is not None and score>=60 else "Mixed fundamentals" if score is not None and score>=40 else "Weak fundamentals" if score is not None else "Insufficient financial data"
    return {"score":score,"label":label,"revenue_growth":rg,"pat_growth":pg,"positives":pos,"risks":risks}

def fetch_financial_intelligence(company):
    url=f"https://ipostation.in/ipo/{_fin_slug(company)}/review"
    req=Request(url,headers={"User-Agent":"Mozilla/5.0"})
    try:
        with urlopen(req,timeout=12) as r: html=r.read().decode("utf-8","ignore")
    except (HTTPError,URLError,TimeoutError,OSError) as exc:
        logger.warning("Financial source unavailable for %r: %s",company,exc)
        return {"available":False,"source":"IPOStation / offer-document data","source_url":url,"reason":"Financial source unavailable for this IPO.","financials":[],"valuation":{},"analysis":{"score":None,"label":"Insufficient financial data","positives":[],"risks":[]}}
    rows=_financial_rows(html); text=_page_text(html); val=_valuation(text)
    return {"available":bool(rows or any(x is not None for x in val.values())),"source":"IPOStation / offer-document data","source_url":url,"as_of":"31 Mar 2026" if "31 Mar 2026" in text else None,"financials":rows,"valuation":val,"analysis":_financial_analysis(rows,val)}

@app.get("/api/ipo/{company}/financials")
def ipo_financials(company: str):
    if not company.strip(): raise HTTPException(400,"Company name can't be empty.")
    try: return fetch_financial_intelligence(company.strip())
    except Exception as exc:
        logger.exception("Financial intelligence failed for %r: %s",company,exc)
        return {"available":False,"source":"IPOStation / offer-document data","source_url":f"https://ipostation.in/ipo/{_fin_slug(company.strip())}/review","reason":"Unable to parse financial data.","financials":[],"valuation":{},"analysis":{"score":None,"label":"Insufficient financial data","positives":[],"risks":[]}}

# ---------------------------------------------------------
# IPO HISTORY
# ---------------------------------------------------------

@app.get("/api/ipo/{company}/history")
def ipo_history(company: str):

    if not company.strip():
        raise HTTPException(
            400,
            "Company name can't be empty.",
        )

    normalized = company.strip().lower()

    db = SessionLocal()

    try:

        rows = (
            db.query(IPOSnapshot)
            .filter(
                IPOSnapshot.normalized_name == normalized
            )
            .order_by(
                IPOSnapshot.fetched_at.asc()
            )
            .all()
        )

        if not rows:
            raise HTTPException(
                404,
                "IPO history not found.",
            )

        # -------------------------------------------------
        # GMP HISTORY
        # -------------------------------------------------

        gmp_points = [
            {
                "value": r.gmp_value,
                "pct": r.gmp_pct,
                "fetched_at": r.fetched_at.isoformat(),
            }
            for r in rows
            if r.gmp_value is not None
            and r.price is not None
            and r.price > 0
        ]

        # -------------------------------------------------
        # SUBSCRIPTION HISTORY
        # -------------------------------------------------

        subscription_points = [
            {
                "overall": r.sub_overall,
                "qib": r.sub_qib,
                "nii": r.sub_nii,
                "retail": r.sub_retail,
                "fetched_at": r.fetched_at.isoformat(),
            }
            for r in rows
            if r.sub_overall is not None
        ]

        # -------------------------------------------------
        # GMP ANALYTICS
        # -------------------------------------------------

        gmp_values = [
            point["value"]
            for point in gmp_points
        ]

        current_gmp = (
            gmp_values[-1]
            if gmp_values
            else None
        )

        previous_gmp = (
            gmp_values[-2]
            if len(gmp_values) >= 2
            else None
        )

        highest_gmp = (
            max(gmp_values)
            if gmp_values
            else None
        )

        lowest_gmp = (
            min(gmp_values)
            if gmp_values
            else None
        )

        if (
            current_gmp is not None
            and previous_gmp is not None
        ):
            gmp_change = round(
                current_gmp - previous_gmp,
                2,
            )
        else:
            gmp_change = 0

        if len(gmp_values) >= 2:
            gmp_change_total = round(
                gmp_values[-1] - gmp_values[0],
                2,
            )
        else:
            gmp_change_total = 0

        # -------------------------------------------------
        # GMP TREND
        # -------------------------------------------------

        if len(gmp_values) < 2:
            trend = "insufficient_data"

        elif gmp_change > 0:
            trend = "rising"

        elif gmp_change < 0:
            trend = "falling"

        else:
            trend = "stable"

        # -------------------------------------------------
        # SUBSCRIPTION ANALYTICS
        # -------------------------------------------------

        subscription_values = [
            point["overall"]
            for point in subscription_points
            if point["overall"] is not None
        ]

        current_subscription = (
            subscription_values[-1]
            if subscription_values
            else None
        )

        previous_subscription = (
            subscription_values[-2]
            if len(subscription_values) >= 2
            else None
        )

        if (
            current_subscription is not None
            and previous_subscription is not None
        ):
            subscription_change = round(
                current_subscription
                - previous_subscription,
                2,
            )
        else:
            subscription_change = 0

        if len(subscription_values) >= 2:
            subscription_change_total = round(
                subscription_values[-1]
                - subscription_values[0],
                2,
            )
        else:
            subscription_change_total = 0

        # -------------------------------------------------
        # LATEST SNAPSHOT
        # -------------------------------------------------

        latest = rows[-1]

        return {
            "company": latest.company,

            "analytics": {
                "current_gmp": current_gmp,
                "previous_gmp": previous_gmp,
                "highest_gmp": highest_gmp,
                "lowest_gmp": lowest_gmp,
                "gmp_change": gmp_change,
                "gmp_change_total": gmp_change_total,
                "trend": trend,

                "current_subscription":
                    current_subscription,

                "previous_subscription":
                    previous_subscription,

                "subscription_change":
                    subscription_change,

                "subscription_change_total":
                    subscription_change_total,

                "snapshot_count":
                    len(rows),

                "gmp_snapshot_count":
                    len(gmp_points),

                "subscription_snapshot_count":
                    len(subscription_points),
            },

            "gmp_history": gmp_points,

            "subscription_history":
                subscription_points,

            "snapshots": [
                {
                    "gmp_value": r.gmp_value,
                    "gmp_pct": r.gmp_pct,
                    "sub_overall": r.sub_overall,
                    "sub_qib": r.sub_qib,
                    "sub_nii": r.sub_nii,
                    "sub_retail": r.sub_retail,
                    "star_rating": r.star_rating,
                    "fetched_at":
                        r.fetched_at.isoformat(),
                }
                for r in rows
            ],
        }

    finally:
        db.close()


# ---------------------------------------------------------
# SINGLE IPO
# ---------------------------------------------------------

@app.get("/api/ipo/{company}")
def get_ipo(company: str):

    if not company.strip():
        raise HTTPException(
            400,
            "Company name can't be empty.",
        )

    try:
        # Keep the existing refresh/cache behaviour. This makes sure the
        # analysis page can still trigger the live scraper when required.
        refreshed = get_or_refresh(company.strip())

        if not refreshed:
            raise HTTPException(
                404,
                "IPO not found.",
            )

        # The dashboard already proves that the database contains the full
        # IPO snapshot (price, dates, lot size, subscription, etc.). The
        # single-IPO service result can occasionally be a partial scraper row,
        # so select the newest COMPLETE snapshot for this company.
        normalized = company.strip().lower()
        db = SessionLocal()
        try:
            rows = (
                db.query(IPOSnapshot)
                .filter(
                    IPOSnapshot.normalized_name == normalized
                )
                .order_by(
                    IPOSnapshot.fetched_at.desc()
                )
                .all()
            )
        finally:
            db.close()

        row = None
        for candidate in rows:
            if is_valid_ipo_snapshot(candidate):
                row = candidate
                break

        # Fallback to the refreshed ORM object if the database lookup cannot
        # find a complete historical row.
        if row is None:
            row = refreshed

        status = calculate_status(
            row.open_date,
            row.close_date,
            row.listing_date,
            row.status,
        )

        # Return a plain dictionary rather than response_model=IPOResponse.
        # This is intentional: the analysis UI also needs these calculated
        # fields, and Pydantic would otherwise discard fields not declared by
        # the older response schema.
        def normalize(value):
            if isinstance(value, (datetime, date)):
                return value.isoformat()
            return value

        # Some scraper paths store 0 for QIB/NII/Retail when the category
        # was not actually returned. Convert those scraper-only zeroes to
        # None so the frontend can correctly display "No data returned".
        # A real zero is preserved if the historical series contains a
        # positive value, proving that the category has been populated.
        sub_qib = normalize_subscription_category(
            rows,
            "sub_qib",
            row.sub_qib,
        )
        sub_nii = normalize_subscription_category(
            rows,
            "sub_nii",
            row.sub_nii,
        )
        sub_retail = normalize_subscription_category(
            rows,
            "sub_retail",
            row.sub_retail,
        )

        payload = {
            "company": row.company,
            "status": status,
            "price": row.price,
            "ipo_size": row.ipo_size,
            "lot_size": row.lot_size,
            "open_date": normalize(row.open_date),
            "close_date": normalize(row.close_date),
            "allotment_date": normalize(row.allotment_date),
            "listing_date": normalize(row.listing_date),
            "anchor": row.anchor,
            "gmp_value": row.gmp_value,
            "gmp_pct": row.gmp_pct,
            "gmp_range_low": row.gmp_range_low,
            "gmp_range_high": row.gmp_range_high,
            "gmp_rating_flames": row.gmp_rating_flames,
            "gmp_as_of": normalize(row.gmp_as_of),
            "sub_overall": row.sub_overall,
            "sub_qib": sub_qib,
            "sub_nii": sub_nii,
            "sub_retail": sub_retail,
            "star_rating": row.star_rating,
            "verdict": row.verdict,
            "confidence": row.confidence,
            "estimated_listing_price": estimated_listing_price(
                row.price,
                row.gmp_value,
            ),
            "estimated_listing_premium": estimated_listing_premium(
                row.price,
                row.gmp_value,
            ),
            "lot_investment": lot_investment(
                row.price,
                row.lot_size,
            ),
            "fetched_at": normalize(row.fetched_at) or datetime.now().isoformat(),
        }

        return payload

    except HTTPException:
        raise

    except Exception as exc:
        logger.exception(
            "IPO lookup failed for %r: %s",
            company,
            exc,
        )

        raise HTTPException(
            500,
            "Unable to fetch IPO data.",
        )


# ---------------------------------------------------------
# IPO SEARCH / AUTOCOMPLETE
# ---------------------------------------------------------

@app.get("/api/search")
def search_ipos(q: str = ""):
    """Search every IPO name stored in the database.

    Unlike /api/dashboard and /api/ipos, this endpoint intentionally
    searches the full snapshot history so an IPO that has already closed
    or is no longer in the latest dashboard window can still appear in
    autocomplete.
    """

    query = str(q or "").strip().lower()

    if len(query) < 1:
        return {"count": 0, "ipos": []}

    db = SessionLocal()

    try:
        latest_rows = latest_valid_rows(db)

        matches = []

        for row in latest_rows:
            company = str(row.company or "").strip()
            if query not in company.lower():
                continue

            matches.append(
                {
                    "company": company,
                    "status": calculate_status(
                        row.open_date,
                        row.close_date,
                        row.listing_date,
                        row.status,
                    ),
                    "price": row.price,
                    "open_date": row.open_date,
                    "close_date": row.close_date,
                    "listing_date": row.listing_date,
                    "fetched_at": row.fetched_at,
                }
            )

        matches.sort(
            key=lambda item: (
                not item["company"].lower().startswith(query),
                item["company"].lower(),
            )
        )

        return {
            "count": len(matches),
            "ipos": matches[:20],
        }

    finally:
        db.close()


# ---------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------

@app.get("/api/dashboard")
def dashboard():

    db = SessionLocal()

    try:

        latest_rows = latest_valid_rows(db, max_rows=200)

        result = []

        for row in latest_rows:

            status = calculate_status(
                row.open_date,
                row.close_date,
                row.listing_date,
                row.status,
            )

            closing_soon = is_closing_soon(
                row.open_date,
                row.close_date,
                row.listing_date,
            )

            result.append(
                {
                    "company":
                        row.company,

                    "status":
                        status,

                    "closing_soon":
                        closing_soon,

                    "price":
                        row.price,

                    "ipo_size":
                        row.ipo_size,

                    "lot_size":
                        row.lot_size,

                    "open_date":
                        row.open_date,

                    "close_date":
                        row.close_date,

                    "allotment_date":
                        row.allotment_date,

                    "listing_date":
                        row.listing_date,

                    "anchor":
                        row.anchor,

                    "gmp_value":
                        row.gmp_value,

                    "gmp_pct":
                        row.gmp_pct,

                    "gmp_range_low":
                        row.gmp_range_low,

                    "gmp_range_high":
                        row.gmp_range_high,

                    "gmp_rating_flames":
                        row.gmp_rating_flames,

                    "gmp_as_of":
                        row.gmp_as_of,

                    "sub_overall":
                        row.sub_overall,

                    "sub_qib":
                        row.sub_qib,

                    "sub_nii":
                        row.sub_nii,

                    "sub_retail":
                        row.sub_retail,

                    "star_rating":
                        row.star_rating,

                    "verdict":
                        row.verdict,

                    "confidence":
                        row.confidence,

                    "estimated_listing_price":
                        estimated_listing_price(
                            row.price,
                            row.gmp_value,
                        ),

                    "estimated_listing_premium":
                        estimated_listing_premium(
                            row.price,
                            row.gmp_value,
                        ),

                    "lot_investment":
                        lot_investment(
                            row.price,
                            row.lot_size,
                        ),

                    "fetched_at":
                        row.fetched_at.isoformat(),
                }
            )

        closing_soon_count = sum(
            1 for item in result
            if item.get("closing_soon") is True
        )

        return {
            "count": len(result),
            "closing_soon_count": closing_soon_count,
            "ipos": result,
        }

    finally:
        db.close()


# ---------------------------------------------------------
# WEEKLY GMP INTELLIGENCE
# ---------------------------------------------------------

def _weekly_company_key(company):
    """Return a stable company key for weekly historical analytics.

    Older snapshots can contain exchange/IPO-type suffixes such as IPOC,
    BSE SMEC, BSE SMEO, NSE SMEC, or NSE SMEO. Those suffixes describe the
    listing/issue type and should not create a second company in weekly
    leaderboards. Keep the display name from the latest snapshot unchanged.
    """
    text = re.sub(r"\s+", " ", str(company or "").strip()).lower()
    if not text:
        return ""

    suffixes = (
        " ipoc",
        " bse smec",
        " nse smec",
        " bse smeo",
        " nse smeo",
        " bse sme",
        " nse sme",
    )

    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if text.endswith(suffix) and len(text) > len(suffix):
                text = text[:-len(suffix)].strip()
                changed = True
                break

    return text


@app.get("/api/gmp-weekly")
def weekly_gmp_intelligence():
    """Return market-level GMP intelligence for the current Monday-Sunday week.

    The dashboard uses the latest valid GMP snapshot for each IPO as the
    current weekly leaderboard. Movers are calculated from the first and
    latest GMP snapshot available for that IPO during the same week.
    """
    db = SessionLocal()

    try:
        now = datetime.now()
        start = datetime.combine(
            now.date() - timedelta(days=now.weekday()),
            datetime.min.time(),
        )
        # The UI is intentionally Monday → today, not Monday → Sunday.
        # This prevents a future date from appearing in the weekly heading.
        end = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())

        rows = (
            db.query(IPOSnapshot)
            .order_by(IPOSnapshot.fetched_at.asc())
            .all()
        )

        weekly_rows = []
        for row in rows:
            fetched_at = getattr(row, "fetched_at", None)
            if not fetched_at:
                continue

            # SQLite normally returns naive datetimes. Handle timezone-aware
            # values too so the endpoint remains safe if the DB changes later.
            compare_time = fetched_at.replace(tzinfo=None) if getattr(fetched_at, "tzinfo", None) else fetched_at
            if not (start <= compare_time < end):
                continue

            if not is_valid_ipo_snapshot(row):
                continue

            if row.gmp_value is None:
                continue

            try:
                gmp = float(row.gmp_value)
            except (TypeError, ValueError):
                continue

            company = str(row.company or "").strip()
            normalized = _weekly_company_key(row.company)
            if not company or not normalized:
                continue

            weekly_rows.append((normalized, row, gmp, compare_time))

        by_company = {}
        for normalized, row, gmp, compare_time in weekly_rows:
            by_company.setdefault(normalized, []).append((row, gmp, compare_time))

        leaders = []
        movers = []

        for normalized, entries in by_company.items():
            entries.sort(key=lambda item: item[2])
            first_row, first_gmp, first_time = entries[0]
            latest_row, latest_gmp, latest_time = entries[-1]

            change = round(latest_gmp - first_gmp, 2)
            change_pct = None
            if first_gmp != 0:
                change_pct = round((change / abs(first_gmp)) * 100, 2)

            # Subscription data can be missing on the first or latest GMP
            # snapshot even when usable subscription values exist elsewhere
            # in the same week's history. Use the first and latest snapshots
            # that actually contain subscription data.
            subscription_entries = []
            for entry_row, entry_gmp, entry_time in entries:
                value = getattr(entry_row, "sub_overall", None)
                try:
                    value = float(value) if value is not None else None
                except (TypeError, ValueError):
                    value = None
                if value is not None:
                    subscription_entries.append((value, entry_time))

            first_subscription = (
                subscription_entries[0][0]
                if subscription_entries
                else None
            )
            latest_subscription = (
                subscription_entries[-1][0]
                if subscription_entries
                else None
            )

            subscription_change = None
            if len(subscription_entries) >= 2:
                subscription_change = round(
                    latest_subscription - first_subscription,
                    2,
                )

            item = {
                "company": str(latest_row.company or first_row.company or "").strip(),
                "gmp_value": latest_gmp,
                "gmp_pct": latest_row.gmp_pct,
                "price": latest_row.price,
                "subscription": latest_subscription,
                "first_subscription": first_subscription,
                "subscription_change": subscription_change,
                "status": calculate_status(
                    latest_row.open_date,
                    latest_row.close_date,
                    latest_row.listing_date,
                    latest_row.status,
                ),
                "first_gmp": first_gmp,
                "latest_gmp": latest_gmp,
                "change": change,
                "change_pct": change_pct,
                "first_fetched_at": first_time.isoformat(),
                "latest_fetched_at": latest_time.isoformat(),
                "snapshot_count": len(entries),
            }
            leaders.append(item)
            movers.append(item)

        leaders.sort(key=lambda item: item["latest_gmp"], reverse=True)
        losers = sorted(leaders, key=lambda item: item["latest_gmp"])[:5]
        leaders = leaders[:5]

        rising_count = sum(1 for item in movers if item["change"] > 0)
        falling_count = sum(1 for item in movers if item["change"] < 0)
        unchanged_count = sum(1 for item in movers if item["change"] == 0)

        movers_up = sorted(
            [item for item in movers if item["change"] > 0],
            key=lambda item: item["change"],
            reverse=True,
        )[:5]
        movers_down = sorted(
            [item for item in movers if item["change"] < 0],
            key=lambda item: item["change"],
        )[:5]

        subscription_items = [
            item for item in movers
            if item.get("subscription") is not None and item.get("subscription") >= 0
        ]
        subscription_leaders = sorted(
            subscription_items,
            key=lambda item: item["subscription"],
            reverse=True,
        )[:5]
        subscription_losers = sorted(
            subscription_items,
            key=lambda item: item["subscription"],
        )[:5]
        subscription_risers = sorted(
            [item for item in subscription_items if item.get("subscription_change") is not None and item["subscription_change"] > 0],
            key=lambda item: item["subscription_change"],
            reverse=True,
        )[:5]
        subscription_falls = sorted(
            [item for item in subscription_items if item.get("subscription_change") is not None and item["subscription_change"] < 0],
            key=lambda item: item["subscription_change"],
        )[:5]

        subscription_points = [item["subscription"] for item in subscription_items]
        subscription_average = round(sum(subscription_points) / len(subscription_points), 2) if subscription_points else None
        subscription_median = round(median(subscription_points), 2) if subscription_points else None
        subscription_above_1 = sum(1 for value in subscription_points if value >= 1)
        subscription_above_10 = sum(1 for value in subscription_points if value >= 10)
        subscription_below_1 = sum(1 for value in subscription_points if value < 1)

        # A demand read is deliberately descriptive rather than an investment recommendation.
        if subscription_points:
            if subscription_above_10 >= max(1, round(len(subscription_points) * 0.25)):
                demand_read = "Strong demand concentration"
            elif subscription_above_1 > subscription_below_1:
                demand_read = "Positive overall demand"
            elif subscription_below_1 > subscription_above_1:
                demand_read = "Weak overall demand"
            else:
                demand_read = "Mixed demand"
        else:
            demand_read = "Subscription data unavailable"

        all_points = [gmp for _, _, gmp, _ in weekly_rows]
        highest_point = None
        lowest_point = None
        if all_points:
            high = max(weekly_rows, key=lambda item: item[2])
            low = min(weekly_rows, key=lambda item: item[2])
            highest_point = {
                "company": str(high[1].company or "").strip(),
                "gmp_value": high[2],
                "fetched_at": high[3].isoformat(),
            }
            lowest_point = {
                "company": str(low[1].company or "").strip(),
                "gmp_value": low[2],
                "fetched_at": low[3].isoformat(),
            }

        return {
            "period": {
                "start": start.date().isoformat(),
                "end": (end - timedelta(days=1)).date().isoformat(),
                "label": f"{start.strftime('%d %b')} – {(end - timedelta(days=1)).strftime('%d %b %Y')}",
            },
            "snapshot_count": len(weekly_rows),
            "ipo_count": len(movers),
            "gmp_available": len(weekly_rows),
            "rising_count": rising_count,
            "falling_count": falling_count,
            "unchanged_count": unchanged_count,
            "average_gmp": round(sum(all_points) / len(all_points), 2) if all_points else None,
            "median_gmp": round(median(all_points), 2) if all_points else None,
            "highest_observed": highest_point,
            "lowest_observed": lowest_point,
            "leaders": leaders,
            "losers": losers,
            "movers_up": movers_up,
            "movers_down": movers_down,
            "subscription_leaders": subscription_leaders,
            "subscription_losers": subscription_losers,
            "subscription_risers": subscription_risers,
            "subscription_falls": subscription_falls,
            "subscription_available_count": len(subscription_items),
            "subscription_average": subscription_average,
            "subscription_median": subscription_median,
            "subscription_above_1_count": subscription_above_1,
            "subscription_above_10_count": subscription_above_10,
            "subscription_below_1_count": subscription_below_1,
            "subscription_demand_read": demand_read,
        }

    finally:
        db.close()


# ---------------------------------------------------------
# TRACKED IPOS
# ---------------------------------------------------------

@app.get("/api/ipos")
def tracked_ipos():

    db = SessionLocal()

    try:

        latest_rows = latest_valid_rows(db, max_rows=200)

        result = []

        for row in latest_rows:

            status = calculate_status(
                row.open_date,
                row.close_date,
                row.listing_date,
                row.status,
            )

            closing_soon = is_closing_soon(
                row.open_date,
                row.close_date,
                row.listing_date,
            )

            result.append(
                {
                    "company":
                        row.company,

                    "status":
                        status,

                    "closing_soon":
                        closing_soon,

                    "price":
                        row.price,

                    "ipo_size":
                        row.ipo_size,

                    "lot_size":
                        row.lot_size,

                    "open_date":
                        row.open_date,

                    "close_date":
                        row.close_date,

                    "allotment_date":
                        row.allotment_date,

                    "listing_date":
                        row.listing_date,

                    "gmp_value":
                        row.gmp_value,

                    "gmp_pct":
                        row.gmp_pct,

                    "sub_overall":
                        row.sub_overall,

                    "sub_qib":
                        row.sub_qib,

                    "sub_nii":
                        row.sub_nii,

                    "sub_retail":
                        row.sub_retail,

                    "star_rating":
                        row.star_rating,

                    "verdict":
                        row.verdict,

                    "confidence":
                        row.confidence,

                    "fetched_at":
                        row.fetched_at.isoformat(),
                }
            )

        return {
            "count": len(result),
            "ipos": result,
        }

    finally:
        db.close()


# ---------------------------------------------------------
# FRONTEND
# ---------------------------------------------------------

app.mount(
    "/static",
    StaticFiles(directory="frontend"),
    name="static",
)


@app.get("/")
def root():
    return FileResponse(
        "frontend/index.html"
    )
