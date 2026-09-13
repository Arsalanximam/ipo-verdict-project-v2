"""
Grey-market IPO scraper with safe matching and bulk discovery.

Features:
- Discovers IPOs from the live GMP table.
- Cleans exchange/status suffixes from IPO names.
- Extracts GMP, price, subscription, lot size and dates.
- Handles changing table layouts more safely.
- Normalizes IPO dates to YYYY-MM-DD.
- Uses date detection based on column headers where possible.
- Rejects invalid/non-IPO rows.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from datetime import datetime
from typing import Optional, Dict, Any, List

from rapidfuzz import fuzz


logger = logging.getLogger(__name__)


# =========================================================
# WINDOWS / PLAYWRIGHT
# =========================================================

if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(
        asyncio.WindowsProactorEventLoopPolicy()
    )


# =========================================================
# SOURCE
# =========================================================

GMP_URL = (
    "https://www.investorgain.com/report/ipo-gmp-live/331/"
)


# =========================================================
# STATUS SUFFIXES
# =========================================================

_STATUS_SUFFIXES = [
    "BSE SMEALLOTTED",
    "BSE SMELISTED",
    "BSE SMECLOSED",
    "BSE SMEOPEN",
    "BSE SMEU",

    "BSE IPOALLOTTED",
    "BSE IPOLISTED",
    "BSE IPOCLOSED",
    "BSE IPOOPEN",
    "BSE IPOU",

    "IPOALLOTTED",
    "IPOLISTED",
    "IPOCLOSED",
    "IPOOPEN",

    "IPOC",
    "IPOU",
    "IPOO",
    "IPOL",

    "SMEALLOTTED",
    "SMELISTED",
    "SMECLOSED",
    "SMEOPEN",
    "SMEU",

    "ALLOTTED",
    "LISTED",
    "CLOSED",
    "OPEN",
]


# =========================================================
# DATE HELPERS
# =========================================================

_DATE_PATTERNS = [
    r"\b\d{1,2}-\d{1,2}-\d{4}\b",
    r"\b\d{1,2}/\d{1,2}/\d{4}\b",
    r"\b\d{1,2}\.\d{1,2}\.\d{4}\b",
    r"\b\d{4}-\d{1,2}-\d{1,2}\b",
    r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b",
    r"\b\d{1,2}\s+[A-Za-z]{3,9},\s*\d{4}\b",
    r"\b[A-Za-z]{3,9}\s+\d{1,2},\s*\d{4}\b",
    # InvestorGain's live table commonly shows dates without a year,
    # e.g. "18-Sep", "22-Sep". The current year is added by
    # _normalize_date() below.
    r"\b\d{1,2}-[A-Za-z]{3}\b",
    r"\b\d{1,2}\s+[A-Za-z]{3}\b",
]


def _extract_date_text(
    value: str,
) -> Optional[str]:
    """
    Find a date inside arbitrary text.
    """

    if not value:
        return None

    text = str(value).strip()

    if not text:
        return None

    for pattern in _DATE_PATTERNS:

        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE,
        )

        if match:
            return match.group(0).strip()

    return None


def _normalize_date(
    value: Optional[str],
) -> Optional[str]:
    """
    Convert supported IPO date formats to YYYY-MM-DD.
    """

    if not value:
        return None

    text = str(value).strip()

    if not text:
        return None

    formats = [
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d.%m.%Y",
        "%Y-%m-%d",

        "%d %b %Y",
        "%d %B %Y",

        "%d %b, %Y",
        "%d %B, %Y",

        "%b %d, %Y",
        "%B %d, %Y",
    ]

    for fmt in formats:

        try:

            parsed = datetime.strptime(
                text,
                fmt,
            )

            return parsed.strftime(
                "%Y-%m-%d"
            )

        except ValueError:
            continue

    # The live GMP table often omits the year (for example "18-Sep").
    # Use the current year for these values.
    for fmt in ("%d-%b", "%d %b"):

        try:

            parsed = datetime.strptime(
                text,
                fmt,
            ).replace(
                year=datetime.now().year
            )

            return parsed.strftime(
                "%Y-%m-%d"
            )

        except ValueError:
            continue

    return None


def _date_from_cell(
    value: str,
) -> Optional[str]:
    """
    Extract and normalize a date from a table cell.
    """

    extracted = _extract_date_text(
        value
    )

    if not extracted:
        return None

    return _normalize_date(
        extracted
    )


# =========================================================
# TABLE LOADING
# =========================================================

def _load_table() -> tuple[
    List[str],
    List[List[str]],
]:
    """Load the live GMP table with HTTP first and browser fallback."""

    # ---------------------------------------------------------
    # 1) Direct HTTP request. This is faster and avoids opening a
    # browser when the site returns the table in normal HTML.
    # ---------------------------------------------------------
    try:
        import requests
        from bs4 import BeautifulSoup

        headers_http = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": GMP_URL,
            "Connection": "keep-alive",
        }

        session = requests.Session()
        response = session.get(
            GMP_URL,
            headers=headers_http,
            timeout=20,
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        best_headers: List[str] = []
        best_rows: List[List[str]] = []

        for table in soup.find_all("table"):
            table_headers = [
                node.get_text(" ", strip=True)
                for node in table.find_all("th")
            ]
            table_rows: List[List[str]] = []

            for tr in table.find_all("tr"):
                cells = [
                    node.get_text(" ", strip=True)
                    for node in tr.find_all(["td", "th"])
                ]
                if cells and cells != table_headers:
                    table_rows.append(cells)

            if len(table_rows) > len(best_rows):
                best_headers = table_headers
                best_rows = table_rows

        valid_like_rows = [
            row for row in best_rows
            if row and row[0].strip().lower() not in (
                "no data available",
                "loading...",
            )
        ]

        if valid_like_rows:
            logger.info(
                "Loaded GMP table via HTTP: %d rows, %d headers",
                len(valid_like_rows),
                len(best_headers),
            )
            return best_headers, valid_like_rows

        logger.info("GMP HTTP response contained no usable table; using browser fallback")

    except Exception as exc:
        logger.info("GMP direct HTTP fetch unavailable; using browser fallback: %s", exc)

    # ---------------------------------------------------------
    # 2) Browser fallback for JavaScript-rendered table.
    # ---------------------------------------------------------
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Playwright is not installed.")
        return [], []

    try:
        with sync_playwright() as pw:
            browser = None
            try:
                browser = pw.chromium.launch(
                    headless=True,
                    channel="chrome",
                    args=[
                        "--disable-http2",
                        "--disable-quic",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
            except Exception:
                browser = pw.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-http2",
                        "--disable-quic",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )

            page = browser.new_page(
                viewport={"width": 1440, "height": 1000},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0.0.0 Safari/537.36"
                ),
            )

            headers: List[str] = []
            data: List[List[str]] = []

            for attempt in range(3):
                try:
                    page.goto(
                        GMP_URL,
                        timeout=30000,
                        wait_until="domcontentloaded",
                    )
                    break
                except Exception as exc:
                    logger.warning(
                        "GMP browser navigation attempt %d/3 failed: %s",
                        attempt + 1,
                        exc,
                    )
                    if attempt == 2:
                        raise
                    time.sleep(2)
                    try:
                        page.close()
                    except Exception:
                        pass
                    page = browser.new_page(
                        viewport={"width": 1440, "height": 1000},
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/140.0.0.0 Safari/537.36"
                        ),
                    )

            for _ in range(12):
                time.sleep(2.5)

                header_nodes = page.query_selector_all("table thead th")
                row_nodes = page.query_selector_all("table tbody tr")

                current_headers = [
                    node.inner_text().strip()
                    for node in header_nodes
                ]
                current_rows: List[List[str]] = []

                for row in row_nodes:
                    cells = [
                        cell.inner_text().strip()
                        for cell in row.query_selector_all("td")
                    ]
                    if cells:
                        current_rows.append(cells)

                current_rows = [
                    row for row in current_rows
                    if row and row[0].strip().lower() not in (
                        "no data available",
                        "loading...",
                    )
                ]

                if current_rows:
                    headers = current_headers
                    data = current_rows
                    break

            browser.close()

            logger.info(
                "Loaded GMP table: %d rows, %d headers",
                len(data),
                len(headers),
            )
            return headers, data

    except Exception as exc:
        logger.exception("GMP table fetch failed: %s", exc)
        return [], []


def _load_rows() -> List[List[str]]:
    """
    Backwards-compatible raw row loader.
    """

    _, rows = _load_table()

    return rows


# =========================================================
# PUBLIC SCRAPER FUNCTIONS
# =========================================================

def fetch_gmp(
    company_name: str,
) -> Optional[Dict[str, Any]]:
    """
    Fetch and fuzzy-match one company.
    """

    headers, rows = _load_table()

    return _best_match(
        company_name,
        rows,
        headers,
    )


def fetch_all_gmp() -> List[Dict[str, Any]]:
    """
    Discover every valid IPO currently present
    in the live GMP table.
    """

    headers, rows = _load_table()

    parsed_rows: List[
        Dict[str, Any]
    ] = []

    seen = set()

    for cells in rows:

        if (
            not cells
            or not cells[0].strip()
        ):
            continue

        row = _parse_row(
            cells,
            headers,
        )

        if not _is_valid_ipo_row(
            row
        ):
            continue

        key = _normalize_name(
            row["matched_name"]
        )

        if (
            not key
            or key in seen
        ):
            continue

        seen.add(key)

        parsed_rows.append(
            row
        )

    logger.info(
        "Discovered %d valid IPO rows from GMP tracker",
        len(parsed_rows),
    )

    return parsed_rows


# =========================================================
# MATCHING
# =========================================================

def _best_match(
    company_name: str,
    rows: List[List[str]],
    headers: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:

    if not rows:
        return None

    query = (
        company_name
        .strip()
        .lower()
    )

    scored: List[
        tuple[int, List[str]]
    ] = []

    for cells in rows:

        if (
            not cells
            or not cells[0].strip()
        ):
            continue

        score = fuzz.partial_ratio(
            query,
            cells[0]
            .strip()
            .lower(),
        )

        if score >= 60:

            scored.append(
                (
                    score,
                    cells,
                )
            )

    if not scored:
        return None

    scored.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    score, cells = scored[0]

    parsed = _parse_row(
        cells,
        headers or [],
    )

    if not _is_valid_ipo_row(
        parsed
    ):

        logger.info(
            "Rejected invalid GMP match for %r: %r (score=%s)",
            company_name,
            cells[0],
            score,
        )

        return None

    return parsed


# =========================================================
# NAME HELPERS
# =========================================================

def _normalize_name(
    value: str,
) -> str:

    return re.sub(
        r"[^a-z0-9]+",
        " ",
        value.lower(),
    ).strip()


def _parse_name(
    raw: str,
) -> tuple[
    str,
    Optional[str],
]:

    original = (
        raw
        .strip()
    )

    upper = (
        original
        .upper()
    )

    for suffix in _STATUS_SUFFIXES:

        if upper.endswith(
            suffix
        ):

            clean = (
                original[
                    : -len(suffix)
                ]
                .strip()
            )

            if "ALLOTTED" in suffix:
                status = "allotted"

            elif "LISTED" in suffix:
                status = "listed"

            elif "CLOSED" in suffix:
                status = "closed"

            elif (
                "OPEN" in suffix
                or suffix.endswith("O")
            ):
                status = "open"

            elif suffix.endswith("U"):
                status = "upcoming"

            else:
                status = None

            return (
                clean,
                status,
            )

    return (
        original,
        None,
    )


# =========================================================
# GMP PARSING
# =========================================================

def _parse_gmp_cell(
    cell: str,
) -> Dict[
    str,
    Optional[float],
]:

    value = None
    pct = None
    low = None
    high = None

    match = re.search(
        r"₹\s*(-?[\d.]+)"
        r"\s*\(([-\d.]+|-)%\)",
        cell,
    )

    if match:

        value = float(
            match.group(1)
        )

        if match.group(2) != "-":

            pct = float(
                match.group(2)
            )

    match_range = re.search(
        r"(-?[\d.]+)"
        r"\s*↓\s*/\s*"
        r"(-?[\d.]+)"
        r"\s*↑",
        cell,
    )

    if match_range:

        low = float(
            match_range.group(1)
        )

        high = float(
            match_range.group(2)
        )

    if value is None:

        fallback = re.search(
            r"-?\d+(?:\.\d+)?",
            cell.replace(
                ",",
                "",
            ),
        )

        if fallback:

            value = float(
                fallback.group()
            )

    return {
        "value": value,
        "pct": pct,
        "range_low": low,
        "range_high": high,
    }


def _first_line(
    cell: str,
) -> str:

    return (
        cell
        .split("\n")[0]
        .strip()
    )


def _to_float(
    text: str,
) -> Optional[float]:

    if not text:
        return None

    match = re.search(
        r"-?\d+(?:\.\d+)?",
        text.replace(
            ",",
            "",
        ),
    )

    return (
        float(match.group())
        if match
        else None
    )


# =========================================================
# HEADER DETECTION
# =========================================================

def _header_index(
    headers: List[str],
    keywords: tuple[str, ...],
) -> Optional[int]:

    if not headers:
        return None

    for index, header in enumerate(
        headers
    ):

        normalized = (
            header
            .strip()
            .lower()
        )

        if all(
            keyword.lower()
            in normalized
            for keyword in keywords
        ):
            return index

    return None


def _find_date_columns(
    headers: List[str],
) -> Dict[str, int]:

    result: Dict[str, int] = {}

    candidates = {
        "open_date": (
            "open",
        ),
        "close_date": (
            "close",
        ),
        "allotment_date": (
            "allotment",
        ),
        "listing_date": (
            "listing",
        ),
    }

    for field, keywords in candidates.items():

        index = _header_index(
            headers,
            keywords,
        )

        if index is not None:

            result[field] = index

    return result


def _find_field_columns(
    headers: List[str],
) -> Dict[str, int]:

    """
    Detect important live-table columns from their headers.

    The website can change column order. Header detection is used
    when possible, while the existing positional layout remains
    the fallback.
    """

    result: Dict[str, int] = {}

    candidates = {
        "name": (("company",), ("name",)),
        "gmp": (("gmp",), ("grey", "market")),
        "rating": (("rating",),),
        "subscription": (("subscription",), ("sub",)),
        "price": (("price",), ("issue", "price")),
        "ipo_size": (("ipo", "size"), ("issue", "size")),
        "lot_size": (("lot", "size"), ("lot",)),
        "as_of": (("updated",), ("as of",)),
        "anchor": (("anchor",),),
    }

    # The live InvestorGain table uses an exact "NAME" header.
    # Handle it explicitly before broader keyword matching so "IPO SIZE"
    # can never be mistaken for the company-name column.
    if headers:
        for index, header in enumerate(headers):
            normalized = re.sub(
                r"\\s+",
                " ",
                header.strip().lower(),
            )
            if normalized == "name":
                result["name"] = index
                break

    for field, patterns in candidates.items():
        # Name was already resolved explicitly above.
        if field == "name" and "name" in result:
            continue

        for pattern in patterns:
            index = _header_index(headers, pattern)
            if index is not None:
                result[field] = index
                break

    return result


# =========================================================
# DATE FALLBACK
# =========================================================

def _find_all_dates(
    cells: List[str],
) -> List[str]:

    dates: List[str] = []

    for cell in cells:

        date = _date_from_cell(
            cell
        )

        if (
            date
            and date not in dates
        ):

            dates.append(
                date
            )

    return dates


# =========================================================
# ROW PARSER
# =========================================================

def _parse_row(
    cells: List[str],
    headers: Optional[List[str]] = None,
) -> Dict[str, Any]:

    """
    Parse one GMP table row.

    Header names are preferred over fixed positions.

    If headers are unavailable, the scraper falls back
    to the original expected layout.
    """

    headers = headers or []

    original_cells = list(
        cells
    )

    # -----------------------------------------------------
    # NAME
    # -----------------------------------------------------

    field_columns = _find_field_columns(
        headers
    )

    def _cell(
        field: str,
        fallback: int,
    ) -> str:
        index = field_columns.get(
            field,
            fallback,
        )
        if (
            index >= 0
            and index < len(cells)
        ):
            return cells[index].strip()
        return ""

    name, status = _parse_name(
        _cell("name", 0)
    )

    # -----------------------------------------------------
    # GMP
    # -----------------------------------------------------

    gmp_cell = _cell(
        "gmp",
        1,
    )

    gmp_info = _parse_gmp_cell(
        gmp_cell
    )

    # -----------------------------------------------------
    # DATE COLUMNS
    # -----------------------------------------------------

    date_columns = _find_date_columns(
        headers
    )

    open_date = None
    close_date = None
    allotment_date = None
    listing_date = None

    if (
        "open_date"
        in date_columns
    ):

        index = date_columns[
            "open_date"
        ]

        if index < len(cells):

            open_date = _date_from_cell(
                cells[index]
            )

    if (
        "close_date"
        in date_columns
    ):

        index = date_columns[
            "close_date"
        ]

        if index < len(cells):

            close_date = _date_from_cell(
                cells[index]
            )

    if (
        "allotment_date"
        in date_columns
    ):

        index = date_columns[
            "allotment_date"
        ]

        if index < len(cells):

            allotment_date = _date_from_cell(
                cells[index]
            )

    if (
        "listing_date"
        in date_columns
    ):

        index = date_columns[
            "listing_date"
        ]

        if index < len(cells):

            listing_date = _date_from_cell(
                cells[index]
            )

    # -----------------------------------------------------
    # ORIGINAL POSITION FALLBACK
    # -----------------------------------------------------

    if not open_date and len(cells) > 7:

        open_date = _date_from_cell(
            cells[7]
        )

    if not close_date and len(cells) > 8:

        close_date = _date_from_cell(
            cells[8]
        )

    if not allotment_date and len(cells) > 9:

        allotment_date = _date_from_cell(
            cells[9]
        )

    if not listing_date and len(cells) > 10:

        listing_date = _date_from_cell(
            cells[10]
        )

    # -----------------------------------------------------
    # LAST RESORT DATE DISCOVERY
    # -----------------------------------------------------

    all_dates = _find_all_dates(
        original_cells
    )

    if not open_date and len(
        all_dates
    ) >= 1:

        open_date = all_dates[0]

    if not close_date and len(
        all_dates
    ) >= 2:

        close_date = all_dates[1]

    if not allotment_date and len(
        all_dates
    ) >= 3:

        allotment_date = all_dates[2]

    if not listing_date and len(
        all_dates
    ) >= 4:

        listing_date = all_dates[3]

    # -----------------------------------------------------
    # OTHER FIELDS
    # -----------------------------------------------------

    subscription_cell = _cell(
        "subscription",
        3,
    )

    price_cell = _cell(
        "price",
        4,
    )

    size_cell = _cell(
        "ipo_size",
        5,
    )

    lot_cell = _cell(
        "lot_size",
        6,
    )

    rating_cell = _cell(
        "rating",
        2,
    )

    as_of_cell = _cell(
        "as_of",
        11,
    )

    anchor_cell = _cell(
        "anchor",
        12,
    )

    # -----------------------------------------------------
    # RETURN
    # -----------------------------------------------------

    return {

        "matched_name": name,

        "status": (
            status
            or "unknown"
        ),

        "value": gmp_info[
            "value"
        ],

        "pct": gmp_info[
            "pct"
        ],

        "range_low": gmp_info[
            "range_low"
        ],

        "range_high": gmp_info[
            "range_high"
        ],

        "as_of": (
            _first_line(
                as_of_cell
            )
            or "live fetch"
        ),

        "rating_flames": (
            rating_cell.count(
                "🔥"
            )
        ),

        "subscription_overall": (
            _to_float(
                subscription_cell
            )
            if "x"
            in subscription_cell.lower()
            else None
        ),

        "price": _to_float(
            price_cell
        ),

        "ipo_size": (
            size_cell.strip()
            or None
        ),

        "lot_size": (
            lot_cell.strip()
            or None
        ),

        "open_date": open_date,

        "close_date": close_date,

        "allotment_date": (
            allotment_date
        ),

        "listing_date": (
            listing_date
        ),

        "anchor": (
            "✅"
            in anchor_cell
        ),
    }


# =========================================================
# VALIDATION
# =========================================================

def _is_valid_ipo_row(
    row: Dict[str, Any],
) -> bool:

    price = row.get(
        "price"
    )

    if (
        price is None
        or price <= 0
    ):
        return False

    company = (
        row.get(
            "matched_name"
        )
        or ""
    ).strip()

    if not company:
        return False

    # NSE is a legitimate IPO in the current IPO calendar.
    # Do not reject the company name "NSE" here. The live-table parser
    # now resolves the exact NAME column first, which prevents the old
    # false NSE match that came from the IPO SIZE column.

    has_date = any(
        row.get(key)
        for key in (
            "open_date",
            "close_date",
            "allotment_date",
            "listing_date",
        )
    )

    has_size = bool(
        row.get(
            "ipo_size"
        )
    )

    has_lot = bool(
        row.get(
            "lot_size"
        )
    )

    # The live GMP table can temporarily omit dates, size, or lot.
    # Keep an otherwise identifiable IPO row instead of discarding it.
    has_market_data = (
        row.get("value") is not None
        or row.get("subscription_overall") is not None
        or has_date
        or has_size
        or has_lot
    )

    return bool(
        has_market_data
    )