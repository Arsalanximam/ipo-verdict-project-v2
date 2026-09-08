"""
Grey Market Premium (and much more) fetcher.

This one page (investorgain's live GMP tracker) turns out to carry
almost everything the dashboard needs in a single table: GMP, a
"rating" (flame icons), the overall subscription multiple, issue
price, IPO size, lot size, open/close dates, allotment date, listing
date, and whether there's an anchor investor. So this single scraper
now replaces most of what the separate NSE scraper was trying to do,
and is the more reliable source of the two.

The table is rendered client-side via JavaScript -- confirmed by
inspecting it directly, the raw HTML just shows "Loading..." until a
background request fills it in, and that fill-in can take up to ~20
seconds on a slow connection. So this polls for real content instead
of using a single fixed wait.

Column layout was confirmed by capturing a live row (see README /
git history for the exact debug output this was built from):
    0 name (may have a status suffix like "IPOU" or "IPOCALLOTTED"
      glued on with no space -- stripped in _parse_name)
    1 gmp   e.g. "₹25 (14.12%)\n18 ↓ / 55 ↑"
    2 rating  fire emoji count, e.g. "🔥🔥🔥"
    3 sub   overall subscription multiple, e.g. "43.4x"
    4 price (₹)  issue price, e.g. "177"
    5 ipo size   e.g. "₹459.72 Cr"
    6 lot   e.g. "84"
    7 open date  e.g. "1-Sep\nGMP: 44"
    8 close date e.g. "3-Sep\nGMP: 18"
    9 allotment date (BOA DT)  e.g. "4-Sep"
    10 listing date  e.g. "8-Sep"
    11 updated-on timestamp
    12 anchor  "✅" or blank

If investorgain changes this layout, the column indices in
_extract_rows are the one place to update.
"""
from __future__ import annotations

import logging
import re
import sys
import time
from typing import Optional, Dict, Any, List

from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

GMP_URL = "https://www.investorgain.com/report/ipo-gmp-live/331/"

# Known status suffixes the site glues directly onto the company name
# with no separating space. Longest-first so e.g. "IPOCALLOTTED" is
# matched before the shorter "IPOC".
_STATUS_SUFFIXES = [
    "BSE SMEALLOTTED", "BSE SMELISTED", "BSE SMECLOSED", "BSE SMEOPEN", "BSE SMEU",
    "IPOALLOTTED", "IPOLISTED", "IPOCLOSED", "IPOOPEN", "IPOCALLOTTED",
    "IPOC", "IPOU", "IPOO", "IPOL",
]


def fetch_gmp(company_name: str) -> Optional[Dict[str, Any]]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning(
            "Playwright not installed -- run `pip install playwright && "
            "playwright install chromium`."
        )
        return None

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(GMP_URL, timeout=30000, wait_until="domcontentloaded")

            data: List[Dict[str, Any]] = []
            for _ in range(10):  # poll up to ~25s for the async table fill-in
                time.sleep(2.5)
                rows = page.query_selector_all("table tbody tr")
                candidates = [
                    [c.inner_text().strip() for c in row.query_selector_all("td")]
                    for row in rows
                ]
                candidates = [c for c in candidates if c]
                if candidates and candidates[0][0].strip().lower() not in (
                    "no data available", "loading..."
                ):
                    data = candidates
                    break

            browser.close()
    except Exception as exc:  # noqa: BLE001 - see module docstring
        logger.warning("GMP fetch failed: %s", exc)
        return None

    return _best_match(company_name, data)


def _best_match(company_name: str, rows: List[List[str]]) -> Optional[Dict[str, Any]]:
    if not rows:
        return None
    best = max(rows, key=lambda cells: fuzz.partial_ratio(company_name.lower(), cells[0].lower()))
    score = fuzz.partial_ratio(company_name.lower(), best[0].lower())
    if score < 60:
        return None
    return _parse_row(best)


def _parse_name(raw: str) -> tuple[str, Optional[str]]:
    """Splits e.g. 'Deepa Jewellers IPOCALLOTTED' -> ('Deepa Jewellers', 'allotted')."""
    for suffix in _STATUS_SUFFIXES:
        if raw.endswith(suffix):
            clean = raw[: -len(suffix)].strip()
            if "ALLOTTED" in suffix:
                status = "allotted"
            elif "LISTED" in suffix:
                status = "listed"
            elif "CLOSED" in suffix:
                status = "closed"
            elif "OPEN" in suffix or suffix.endswith("O"):
                status = "open"
            elif suffix.endswith("U"):
                status = "upcoming"
            else:
                status = None
            return clean, status
    return raw.strip(), None


def _parse_gmp_cell(cell: str) -> Dict[str, Optional[float]]:
    """Parses '₹25 (14.12%)\\n18 ↓ / 55 ↑' into value/pct/range."""
    value = pct = low = high = None
    m = re.search(r"₹\s*(-?[\d.]+)\s*\(([-\d.]+|-)%\)", cell)
    if m:
        value = float(m.group(1))
        pct = float(m.group(2)) if m.group(2) != "-" else None
    m2 = re.search(r"(-?[\d.]+)\s*↓\s*/\s*(-?[\d.]+)\s*↑", cell)
    if m2:
        low, high = float(m2.group(1)), float(m2.group(2))
    if value is None:
        # Fallback: just grab the first number in the cell.
        m3 = re.search(r"-?\d+(\.\d+)?", cell.replace(",", ""))
        if m3:
            value = float(m3.group())
    return {"value": value, "pct": pct, "range_low": low, "range_high": high}


def _first_line(cell: str) -> str:
    return cell.split("\n")[0].strip()


def _to_float(text: str) -> Optional[float]:
    m = re.search(r"-?\d+(\.\d+)?", text.replace(",", ""))
    return float(m.group()) if m else None


def _parse_row(cells: List[str]) -> Dict[str, Any]:
    # Defensive: pad in case a row has fewer cells than expected.
    cells = cells + [""] * max(0, 13 - len(cells))

    name, status = _parse_name(cells[0])
    gmp_info = _parse_gmp_cell(cells[1])

    return {
        "matched_name": name,
        "status": status or "unknown",
        "value": gmp_info["value"],
        "pct": gmp_info["pct"],
        "range_low": gmp_info["range_low"],
        "range_high": gmp_info["range_high"],
        "as_of": _first_line(cells[11]) or "live fetch",
        "rating_flames": cells[2].count("🔥"),
        "subscription_overall": _to_float(cells[3]) if "x" in cells[3].lower() else None,
        "price": _to_float(cells[4]),
        "ipo_size": cells[5].strip() or None,
        "lot_size": cells[6].strip() or None,
        "open_date": _first_line(cells[7]) or None,
        "close_date": _first_line(cells[8]) or None,
        "allotment_date": _first_line(cells[9]) or None,
        "listing_date": _first_line(cells[10]) or None,
        "anchor": "✅" in cells[12],
    }
