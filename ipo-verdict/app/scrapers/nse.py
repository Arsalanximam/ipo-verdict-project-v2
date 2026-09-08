"""
NSE subscription-data fetcher.

NSE India does not publish a documented public API, but its own website
calls internal JSON endpoints under nseindia.com/api/*. Those endpoints
reject plain script requests (no browser, no cookies) with a 401/403,
so the standard community pattern -- used by libraries like
bennythadikaran/NseIndiaApi -- is:

    1. GET the homepage first with browser-like headers, to receive
       session cookies (NSE sets anti-bot cookies on first load).
    2. Reuse that same requests.Session (cookies included) to call the
       actual data endpoint.

This is inherently a bit fragile: NSE can and does change endpoint
paths, cookie logic, and rate limits without notice. Treat failures
here as expected, not as bugs -- the code is written to degrade
gracefully (return None) rather than crash the whole request.
"""
from __future__ import annotations

import logging
from typing import Optional, Dict, Any

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://www.nseindia.com"
# Unofficial, undocumented endpoint used by NSE's own IPO subscription
# widget. Verify this still resolves by hitting it in a browser's
# network tab (devtools) if it ever stops returning data -- endpoint
# paths on NSE shift periodically.
IPO_SUBSCRIPTION_ENDPOINT = f"{BASE_URL}/api/ipo-active-category"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{BASE_URL}/market-data/all-upcoming-issues-ipo",
}


def _warmed_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    # Hitting the homepage first is what actually sets the cookies NSE's
    # API checks for. Without this step the API call below will 401/403.
    session.get(BASE_URL, timeout=10)
    return session


def fetch_subscription(company_name: str) -> Optional[Dict[str, Any]]:
    """
    Returns a dict like:
        {"qib": 12.4, "nii": 8.2, "retail": 3.1, "overall": 7.5}
    or None if the company wasn't found / the endpoint failed.
    """
    try:
        session = _warmed_session()
        resp = session.get(IPO_SUBSCRIPTION_ENDPOINT, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see module docstring
        logger.warning("NSE subscription fetch failed: %s", exc)
        return None

    # The shape of NSE's response varies by endpoint/version. Adjust this
    # matching logic once you've inspected a real response in devtools --
    # this is written defensively against a couple of plausible shapes.
    rows = payload if isinstance(payload, list) else payload.get("data", [])
    target = company_name.strip().lower()

    for row in rows:
        name = str(row.get("companyName") or row.get("symbol") or "").lower()
        if target in name or name in target:
            return {
                "qib": _to_float(row.get("qib") or row.get("QIB")),
                "nii": _to_float(row.get("nii") or row.get("NII") or row.get("hni")),
                "retail": _to_float(row.get("retail") or row.get("RII")),
                "overall": _to_float(row.get("total") or row.get("overallSubscription")),
            }
    return None


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(str(value).replace("x", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None
