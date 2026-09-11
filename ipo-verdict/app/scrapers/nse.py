"""Robust NSE IPO subscription scraper.

Primary source: official NSE IPO pages.
The old implementation depended on the undocumented ``/api/ipo-active-category``
JSON endpoint. When NSE returns an HTML challenge/error page, ``resp.json()``
raises and subscription data disappears from IPO Verdict.

This version keeps the fast JSON path, then falls back to a real browser session
against NSE's public IPO pages. Browser results are cached briefly so a scheduler
refresh of many IPOs does not launch a browser for every company.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Dict, Optional, List, Tuple

import requests
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

BASE_URL = "https://www.nseindia.com"
IPO_LIST_URL = f"{BASE_URL}/market-data/all-upcoming-issues-ipo"
IPO_SUBSCRIPTION_ENDPOINT = f"{BASE_URL}/api/ipo-active-category"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": IPO_LIST_URL,
    "Connection": "keep-alive",
}

_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_LOADED_AT = 0.0
_CACHE_TTL_SECONDS = 180.0
_JSON_FAILED_UNTIL = 0.0


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text or text in {"-", "—", "na", "n/a", "null", "none"}:
        return None
    text = text.replace(",", "").replace("x", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _normalize_name(value: str) -> str:
    text = str(value or "").lower()
    text = re.sub(r"\b(limited|ltd|india|private|pvt)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _match_score(target: str, candidate: str) -> int:
    a = _normalize_name(target)
    b = _normalize_name(candidate)
    if not a or not b:
        return 0
    if a == b:
        return 100
    if a in b or b in a:
        return 96
    return int(max(fuzz.token_set_ratio(a, b), fuzz.partial_ratio(a, b)))


def _extract_row_value(row: Dict[str, Any], *names: str) -> Any:
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    for key, value in lowered.items():
        if any(name.lower() in key for name in names):
            return value
    return None


def _parse_json_rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "rows", "result", "results", "records"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            nested = _parse_json_rows(value)
            if nested:
                return nested
    return []


def _parse_json_subscription(target: str, rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    best = None
    best_score = 0
    for row in rows:
        candidate = (
            row.get("companyName") or row.get("company") or row.get("company_name")
            or row.get("name") or row.get("symbol") or ""
        )
        score = _match_score(target, str(candidate))
        if score > best_score:
            best_score = score
            best = row
    if not best or best_score < 70:
        return None

    qib = _to_float(_extract_row_value(best, "qib", "QIB"))
    nii = _to_float(_extract_row_value(best, "nii", "NII", "hni"))
    retail = _to_float(_extract_row_value(best, "retail", "RII"))
    overall = _to_float(
        _extract_row_value(
            best,
            "subscription",
            "overallSubscription",
            "overall",
            "totalSubscription",
            "total",
        )
    )
    if overall is None:
        offered = _to_float(_extract_row_value(best, "offered", "reserved", "sharesOffered"))
        bids = _to_float(_extract_row_value(best, "bids", "sharesBid", "bidQuantity"))
        if offered and offered > 0 and bids is not None:
            overall = round(bids / offered, 2)

    result = {"qib": qib, "nii": nii, "retail": retail, "overall": overall}
    return result if any(v is not None for v in result.values()) else None


def _warmed_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    session.get(BASE_URL, timeout=12)
    session.get(IPO_LIST_URL, timeout=12)
    return session


def _try_json_endpoint(company_name: str) -> Optional[Dict[str, Any]]:
    global _JSON_FAILED_UNTIL
    if time.time() < _JSON_FAILED_UNTIL:
        return None
    try:
        session = _warmed_session()
        response = session.get(IPO_SUBSCRIPTION_ENDPOINT, timeout=12)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            logger.info("NSE subscription endpoint returned non-JSON content; using browser fallback")
            return None
        payload = response.json()
        return _parse_json_subscription(company_name, _parse_json_rows(payload))
    except Exception as exc:
        _JSON_FAILED_UNTIL = time.time() + _CACHE_TTL_SECONDS
        logger.info("NSE JSON subscription path unavailable: %s", exc)
        return None


def _parse_rendered_table(page) -> List[Dict[str, Any]]:
    """Read the public NSE IPO list table from the rendered page."""
    rows: List[Dict[str, Any]] = []

    tables = page.locator("table")

    try:
        table_count = tables.count()
    except Exception:
        return rows

    for table_index in range(table_count):
        table = tables.nth(table_index)

        try:
            header_cells = [
                x.strip()
                for x in table.locator("thead th, thead td").all_inner_texts()
            ]
        except Exception:
            header_cells = []

        # Skip unrelated NSE tables.
        header_text = " ".join(header_cells).lower()
        if header_cells and not any(
            key in header_text
            for key in (
                "company",
                "subscription",
                "ipo",
                "issue",
            )
        ):
            continue

        try:
            trs = table.locator("tbody tr").all()
        except Exception:
            continue

        for tr in trs:
            try:
                cells = [
                    x.strip()
                    for x in tr.locator("th,td").all_inner_texts()
                ]
            except Exception:
                continue

            if len(cells) < 2:
                continue

            links = []
            try:
                links = [
                    a.get_attribute("href") or ""
                    for a in tr.locator("a").all()
                ]
            except Exception:
                pass

            rows.append(
                {
                    "cells": cells,
                    "links": links,
                    "headers": header_cells,
                }
            )

    return rows


def _find_subscription_from_cells(
    cells: List[str],
    headers: Optional[List[str]] = None,
) -> Optional[float]:
    """Extract the displayed overall subscription multiple safely."""

    headers = headers or []

    # Prefer a column whose header explicitly says Subscription.
    for index, header in enumerate(headers):
        if (
            "subscription" in header.lower()
            and index < len(cells)
        ):
            value = _to_float(cells[index])
            if value is not None:
                return value

    # NSE currently places subscription after offered/reserved and bids.
    # Keep the existing reverse scan as a fallback.
    for value in reversed(cells):
        number = _to_float(value)
        if number is not None:
            return number

    return None


def _extract_symbol(links: List[str]) -> Optional[str]:
    for href in links:
        match = re.search(
            r"[?&]symbol=([^&#]+)",
            href,
            re.I,
        )
        if match:
            return match.group(1)

        # Some rendered NSE links expose the symbol as a path segment.
        match = re.search(
            r"/([A-Z][A-Z0-9_-]{2,})/?(?:\?|#|$)",
            href,
        )
        if match:
            return match.group(1)

    return None


def _parse_category_table(page) -> Dict[str, Optional[float]]:
    """Extract QIB/NII/Retail subscription values from NSE issue information."""

    result = {
        "qib": None,
        "nii": None,
        "retail": None,
    }

    tables = page.locator("table")

    try:
        table_count = tables.count()
    except Exception:
        return result

    aliases = {
        "qib": (
            "qib",
            "qualified institutional",
            "qualified institutional buyers",
        ),
        "nii": (
            "nii",
            "non institutional",
            "non-institutional",
            "hni",
        ),
        "retail": (
            "retail",
            "rii",
            "individual investor",
        ),
    }

    for table_index in range(table_count):
        table = tables.nth(table_index)

        try:
            rows = table.locator("tr").all()
        except Exception:
            continue

        for tr in rows:
            try:
                cells = [
                    x.strip()
                    for x in tr.locator("th,td").all_inner_texts()
                ]
            except Exception:
                continue

            if len(cells) < 2:
                continue

            joined = " ".join(cells).lower()

            for key, names in aliases.items():
                if not any(
                    re.search(
                        r"\b" + re.escape(name) + r"\b",
                        joined,
                    )
                    for name in names
                ):
                    continue

                # Prefer explicit x-form values because issue-information
                # tables may also contain share quantities and percentages.
                x_values = []
                for cell in cells:
                    match = re.search(
                        r"(-?\d+(?:\.\d+)?)\s*x\b",
                        cell.lower(),
                    )
                    if match:
                        try:
                            x_values.append(
                                float(match.group(1))
                            )
                        except ValueError:
                            pass

                if x_values:
                    result[key] = x_values[-1]
                    continue

                # Fallback to numeric cells, using the last numeric value
                # as the subscription multiple.
                candidates = [
                    _to_float(cell)
                    for cell in cells
                ]
                candidates = [
                    value
                    for value in candidates
                    if value is not None
                ]

                if candidates:
                    result[key] = candidates[-1]

    return result



def _discover_issue_symbols(page) -> Dict[str, str]:
    """Discover NSE IPO symbols from the Issue Information selector.

    The public IPO list does not always expose a symbol link. NSE's Issue
    Information page does expose the issue selector, so use that as a
    fallback source instead of silently losing category-level data.
    """
    mapping: Dict[str, str] = {}

    try:
        selects = page.locator("select")
        select_count = selects.count()
    except Exception:
        return mapping

    for index in range(select_count):
        select = selects.nth(index)
        try:
            options = select.locator("option").all()
        except Exception:
            continue

        for option in options:
            try:
                text = (option.inner_text() or "").strip()
                value = (option.get_attribute("value") or "").strip()
            except Exception:
                continue

            if not text and not value:
                continue

            candidates = [value, text]
            symbol = None
            for candidate in candidates:
                candidate_clean = str(candidate).strip()
                if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,24}", candidate_clean):
                    symbol = candidate_clean
                    break

            if not symbol:
                continue

            # Keep both the visible option text and symbol as searchable
            # aliases. This handles selectors that show company names as
            # well as selectors that show only NSE symbols.
            mapping[_normalize_name(text)] = symbol
            mapping[_normalize_name(value)] = symbol

    return mapping


def _parse_html_tables(html: str) -> List[Dict[str, Any]]:
    """Parse NSE HTML tables without requiring Playwright."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []

    soup = BeautifulSoup(html or "", "html.parser")
    rows: List[Dict[str, Any]] = []
    for table in soup.find_all("table"):
        headers = [x.get_text(" ", strip=True) for x in table.find_all("th")]
        header_text = " ".join(headers).lower()
        if headers and not any(k in header_text for k in ("company", "subscription", "ipo", "issue")):
            continue
        body_rows = table.find_all("tr")
        for tr in body_rows:
            cells = [x.get_text(" ", strip=True) for x in tr.find_all(["th", "td"])]
            if len(cells) < 2:
                continue
            links = [a.get("href") or "" for a in tr.find_all("a")]
            rows.append({"cells": cells, "links": links, "headers": headers})
    return rows


def _requests_load_all() -> Dict[str, Dict[str, Any]]:
    """Try the public NSE HTML pages directly before launching a browser."""
    discovered: Dict[str, Dict[str, Any]] = {}
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        session.get(BASE_URL, timeout=12)
        response = session.get(IPO_LIST_URL, timeout=20)
        if response.status_code != 200:
            logger.warning("NSE IPO HTML returned HTTP %s", response.status_code)
            return {}

        table_rows = _parse_html_tables(response.text)
        for item in table_rows:
            cells = item["cells"]
            company = cells[0].strip() if cells else ""
            if not company or company.lower() in {"company name", "company"}:
                continue
            overall = _find_subscription_from_cells(cells, item.get("headers"))
            if overall is None:
                continue
            symbol = _extract_symbol(item.get("links", []))
            discovered[_normalize_name(company)] = {
                "company": company,
                "symbol": symbol,
                "overall": overall,
                "qib": None,
                "nii": None,
                "retail": None,
            }

        # The Issue Information page exposes issue symbols in its selector.
        try:
            info = session.get(f"{BASE_URL}/market-data/issue-information", timeout=20)
            if info.status_code == 200:
                soup_text = info.text
                try:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(soup_text, "html.parser")
                    for option in soup.find_all("option"):
                        text = option.get_text(" ", strip=True)
                        value = (option.get("value") or "").strip()
                        symbol = None
                        for candidate in (value, text):
                            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,24}", candidate):
                                symbol = candidate
                                break
                        if not symbol:
                            continue
                        for alias in (text, value):
                            normalized = _normalize_name(alias)
                            if not normalized:
                                continue
                            best_key = None
                            best_score = 0
                            for key, row in discovered.items():
                                score = _match_score(normalized, key)
                                if score > best_score:
                                    best_score = score
                                    best_key = key
                            if best_key and best_score >= 70:
                                discovered[best_key]["symbol"] = symbol
                except Exception as exc:
                    logger.debug("NSE HTML issue selector parse failed: %s", exc)
        except Exception as exc:
            logger.debug("NSE HTML issue information request failed: %s", exc)

        # Fetch category tables directly as HTML for rows with symbols.
        for item in discovered.values():
            symbol = item.get("symbol")
            if not symbol:
                continue
            try:
                detail_url = f"{BASE_URL}/market-data/issue-information?symbol={symbol}&series=EQ&type=Active"
                detail = session.get(detail_url, timeout=20)
                if detail.status_code != 200:
                    continue
                tables = _parse_html_tables(detail.text)
                result = {"qib": None, "nii": None, "retail": None}
                # Reuse the same category semantics as the browser parser.
                for table in tables:
                    for cells in _html_table_cell_rows(detail.text):
                        joined = " ".join(cells).lower()
                        aliases = {
                            "qib": ("qib", "qualified institutional"),
                            "nii": ("nii", "non institutional", "non-institutional", "hni"),
                            "retail": ("retail", "rii", "individual investor"),
                        }
                        for key, names in aliases.items():
                            if not any(re.search(r"\b" + re.escape(name) + r"\b", joined) for name in names):
                                continue
                            x_values = []
                            for cell in cells:
                                m = re.search(r"(-?\d+(?:\.\d+)?)\s*x\b", cell.lower())
                                if m:
                                    x_values.append(float(m.group(1)))
                            if x_values:
                                result[key] = x_values[-1]
                            else:
                                nums = [_to_float(c) for c in cells]
                                nums = [n for n in nums if n is not None]
                                if nums:
                                    result[key] = nums[-1]
                item.update(result)
            except Exception as exc:
                logger.debug("NSE requests category fetch failed for %s: %s", item.get("company"), exc)

        category_complete = sum(1 for item in discovered.values() if any(item.get(k) is not None for k in ("qib", "nii", "retail")))
        logger.info("NSE direct HTML subscription data: %d IPO rows; category data: %d rows", len(discovered), category_complete)
        if discovered:
            return discovered
    except Exception as exc:
        logger.warning("NSE direct HTML scrape failed: %s", exc)
    return {}


def _html_table_cell_rows(html: str) -> List[List[str]]:
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html or "", "html.parser")
        return [
            [x.get_text(" ", strip=True) for x in tr.find_all(["th", "td"])]
            for tr in soup.find_all("tr")
            if len(tr.find_all(["th", "td"])) >= 2
        ]
    except Exception:
        return []


def _browser_load_all() -> Dict[str, Dict[str, Any]]:
    global _CACHE_LOADED_AT
    now = time.time()
    with _CACHE_LOCK:
        if _CACHE and now - _CACHE_LOADED_AT < _CACHE_TTL_SECONDS:
            return dict(_CACHE)

    direct = _requests_load_all()
    if direct:
        with _CACHE_LOCK:
            _CACHE.clear()
            _CACHE.update(direct)
            _CACHE_LOADED_AT = time.time()
        return dict(direct)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Playwright is not installed; NSE browser fallback unavailable")
        return {}

    discovered: Dict[str, Dict[str, Any]] = {}
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(
                    headless=True,
                    channel="chrome",
                    args=[
                        "--disable-http2",
                        "--disable-quic",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-features=IsolateOrigins,site-per-process",
                    ],
                )
                logger.info("NSE browser fallback using installed Chrome")
            except Exception as chrome_exc:
                logger.info("Installed Chrome unavailable; using bundled Chromium: %s", chrome_exc)
                browser = pw.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-http2",
                        "--disable-quic",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-features=IsolateOrigins,site-per-process",
                    ],
                )
            page = browser.new_page(
                user_agent=HEADERS["User-Agent"],
                locale="en-IN",
                viewport={"width": 1440, "height": 1000},
            )
            # Do not require a separate homepage navigation. NSE sometimes
            # resets automated Chromium connections on the root URL. Go
            # directly to the IPO table and retry with a fresh page if needed.
            last_nav_error = None
            for attempt in range(1, 4):
                try:
                    page.goto(
                        IPO_LIST_URL,
                        timeout=20000,
                        wait_until="commit",
                    )
                    try:
                        page.wait_for_load_state(
                            "domcontentloaded",
                            timeout=15000,
                        )
                    except Exception:
                        pass
                    last_nav_error = None
                    break
                except Exception as nav_exc:
                    last_nav_error = nav_exc
                    logger.warning(
                        "NSE browser navigation attempt %d/3 failed: %s",
                        attempt,
                        nav_exc,
                    )
                    if attempt < 3:
                        try:
                            page.close()
                        except Exception:
                            pass
                        page = browser.new_page(
                            user_agent=HEADERS["User-Agent"],
                            locale="en-IN",
                            viewport={"width": 1440, "height": 1000},
                        )
                        try:
                            page.wait_for_timeout(1500 * attempt)
                        except Exception:
                            pass

            if last_nav_error is not None:
                raise last_nav_error
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            try:
                page.wait_for_timeout(2500)
            except Exception:
                pass

            table_rows = _parse_rendered_table(page)

            # NSE's IPO list can expose company names and subscription values
            # without an href containing the issue symbol. Load the public
            # Issue Information selector once and use it as a symbol fallback.
            issue_symbol_map: Dict[str, str] = {}
            try:
                symbol_page = browser.new_page(
                    user_agent=HEADERS["User-Agent"],
                    locale="en-IN",
                    viewport={"width": 1280, "height": 900},
                )
                symbol_page.goto(
                    f"{BASE_URL}/market-data/issue-information",
                    timeout=20000,
                    wait_until="domcontentloaded",
                )
                try:
                    symbol_page.wait_for_timeout(1800)
                except Exception:
                    pass
                issue_symbol_map = _discover_issue_symbols(symbol_page)
                logger.info(
                    "NSE Issue Information selector: %d symbol aliases discovered",
                    len(issue_symbol_map),
                )
                symbol_page.close()
            except Exception as exc:
                logger.debug("NSE symbol selector discovery failed: %s", exc)

            for item in table_rows:
                cells = item["cells"]
                if not cells:
                    continue
                company = cells[0]
                if not company or company.lower() in {"company name", "company"}:
                    continue
                overall = _find_subscription_from_cells(
                    cells,
                    item.get("headers"),
                )
                if overall is None:
                    continue

                symbol = _extract_symbol(
                    item["links"]
                )
                if not symbol:
                    normalized_company = _normalize_name(company)
                    symbol = issue_symbol_map.get(normalized_company)
                    if not symbol:
                        best_symbol_score = 0
                        for alias, alias_symbol in issue_symbol_map.items():
                            score = _match_score(company, alias)
                            if score > best_symbol_score:
                                best_symbol_score = score
                                symbol = alias_symbol
                        if best_symbol_score < 70:
                            symbol = None

                discovered[_normalize_name(company)] = {
                    "company": company,
                    "symbol": symbol,
                    "overall": overall,
                    "qib": None,
                    "nii": None,
                    "retail": None,
                }

            # Try category-level data only for rows that expose an NSE symbol.
            # This is deliberately limited to the current IPO table so the
            # scheduler does not crawl arbitrary NSE pages.
            for key, item in list(discovered.items()):
                symbol = item.get("symbol")
                if not symbol:
                    continue
                try:
                    detail_url = f"{BASE_URL}/market-data/issue-information?symbol={symbol}&series=EQ&type=Active"
                    detail = browser.new_page(
                        user_agent=HEADERS["User-Agent"],
                        locale="en-IN",
                        viewport={"width": 1280, "height": 900},
                    )
                    detail.goto(detail_url, timeout=20000, wait_until="domcontentloaded")
                    try:
                        detail.wait_for_timeout(1200)
                    except Exception:
                        pass
                    categories = _parse_category_table(detail)
                    item.update(categories)
                    detail.close()
                except Exception as exc:
                    logger.debug("NSE category fetch failed for %s: %s", item.get("company"), exc)

            category_complete = sum(
                1
                for item in discovered.values()
                if any(
                    item.get(key) is not None
                    for key in ("qib", "nii", "retail")
                )
            )

            logger.info(
                "NSE category subscription data: %d/%d IPO rows",
                category_complete,
                len(discovered),
            )

            browser.close()
    except Exception as exc:
        logger.warning("NSE browser subscription scrape failed: %s", exc)
        return {}

    with _CACHE_LOCK:
        _CACHE.clear()
        _CACHE.update(discovered)
        _CACHE_LOADED_AT = time.time()
    logger.info("NSE browser subscription scrape: %d IPO rows", len(discovered))
    return dict(discovered)




# Public fallback: NSE may return an anti-bot HTML shell to automated
# requests/browser sessions. IPOWatch publishes the same live NSE/BSE
# category subscription figures in a simple HTML table.
IPOWATCH_SUBSCRIPTION_URL = "https://ipowatch.in/ipo-subscription-status-today/"

def _external_subscription_table() -> List[Dict[str, Any]]:
    """Load current category subscription rows from IPOWatch as fallback."""
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": HEADERS["User-Agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        response = session.get(IPOWATCH_SUBSCRIPTION_URL, timeout=15)
        response.raise_for_status()
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            logger.warning("BeautifulSoup is unavailable; IPOWatch fallback disabled")
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        results = []
        for table in soup.find_all("table"):
            trs = table.find_all("tr")
            if not trs:
                continue
            header = [c.get_text(" ", strip=True).lower() for c in trs[0].find_all(["th", "td"])]
            joined = " | ".join(header)
            if "qib" not in joined or "retail" not in joined or "total" not in joined:
                continue
            indexes = {}
            for i, h in enumerate(header):
                if "qib" in h and "qib" not in indexes: indexes["qib"] = i
                elif ("nii" in h or "hni" in h) and "nii" not in indexes: indexes["nii"] = i
                elif "retail" in h or "rii" in h: indexes["retail"] = i
                elif "total" in h: indexes["overall"] = i
                elif h == "ipo" or "company" in h or "name" in h: indexes["company"] = i
            if "company" not in indexes:
                indexes["company"] = 0
            for tr in trs[1:]:
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                if len(cells) < 4:
                    continue
                def val(key):
                    i = indexes.get(key)
                    if i is None or i >= len(cells): return None
                    return _to_float(cells[i])
                company = cells[indexes["company"]].strip()
                if not company or company.lower() in {"ipo", "company", "company name"}:
                    continue
                results.append({"company": company, "qib": val("qib"), "nii": val("nii"), "retail": val("retail"), "overall": val("overall"), "source": "IPOWatch (NSE/BSE data)"})
            if results:
                return results
    except Exception as exc:
        logger.info("IPOWatch subscription fallback unavailable: %s", exc)
    return []

def _external_subscription(company_name: str) -> Optional[Dict[str, Any]]:
    rows = _external_subscription_table()
    best = None
    best_score = 0
    for row in rows:
        score = _match_score(company_name, row.get("company", ""))
        if score > best_score:
            best_score = score
            best = row
    if not best or best_score < 70:
        return None
    return best


def fetch_subscription(company_name: str) -> Optional[Dict[str, Any]]:
    """Return subscription data without launching the unstable NSE browser."""
    company_name = str(company_name or "").strip()
    if not company_name:
        return None

    quick = _try_json_endpoint(company_name)
    if quick and any(quick.get(k) is not None for k in ("qib", "nii", "retail")):
        return quick

    external = _external_subscription(company_name)
    if external and any(external.get(k) is not None for k in ("qib", "nii", "retail", "overall")):
        return {
            "qib": external.get("qib"),
            "nii": external.get("nii"),
            "retail": external.get("retail"),
            "overall": external.get("overall"),
            "source": external.get("source", "public subscription table"),
        }

    # Never invoke the NSE Playwright fallback here. NSE is resetting
    # automated browser connections and it blocks the scheduler for minutes.
    return quick
