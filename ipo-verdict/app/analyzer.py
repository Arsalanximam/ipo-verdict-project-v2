"""
Turns raw scraped numbers into a star rating and a plain-language
verdict. The scoring is a transparent, documented heuristic -- not a
trained model -- and it deliberately weights official data (subscription,
financials) above unofficial sentiment (GMP). See README for the
reasoning.

If ANTHROPIC_API_KEY is set, the final verdict paragraph is written by
Claude via the Anthropic Python SDK (server-side -- the key never
touches the browser). Without a key, a template-based verdict is used
instead so the app still fully works.
"""
from __future__ import annotations

from typing import Optional, Dict, Any

from app.config import LLM_ENABLED, ANTHROPIC_API_KEY

WEIGHTS = {
    "subscription": 0.30,
    "financials": 0.28,
    "valuation": 0.20,
    "anchor": 0.12,
    "gmp": 0.10,
}


def score_subscription(overall: Optional[float], qib: Optional[float]) -> Optional[float]:
    primary = overall if overall is not None else qib
    if primary is None:
        return None
    if primary < 1:
        s = 0.1
    elif primary < 3:
        s = 0.4
    elif primary < 10:
        s = 0.65
    elif primary < 25:
        s = 0.85
    else:
        s = 0.95
    if qib is not None and qib >= 10:
        s = min(1.0, s + 0.08)
    return s


def score_financials(rev_growth, margin, debt_equity, roe) -> Optional[float]:
    parts = [rev_growth, margin, debt_equity, roe]
    if all(p is None for p in parts):
        return None
    hits, checked = 0.0, 0
    if rev_growth is not None:
        checked += 1
        hits += 1 if rev_growth >= 15 else 0.5 if rev_growth >= 5 else 0
    if margin is not None:
        checked += 1
        hits += 1 if margin >= 10 else 0.5 if margin >= 4 else 0
    if debt_equity is not None:
        checked += 1
        hits += 1 if debt_equity <= 0.75 else 0.5 if debt_equity <= 1.5 else 0
    if roe is not None:
        checked += 1
        hits += 1 if roe >= 15 else 0.5 if roe >= 8 else 0
    return hits / checked if checked else None


def score_valuation(ipo_pe, peer_pe) -> Optional[float]:
    if ipo_pe is None or peer_pe in (None, 0):
        return None
    ratio = ipo_pe / peer_pe
    if ratio <= 0.9:
        return 0.9
    if ratio <= 1.1:
        return 0.65
    if ratio <= 1.4:
        return 0.35
    return 0.12


def score_anchor(anchor_pct, marquee: bool) -> Optional[float]:
    if anchor_pct is None:
        return None
    s = 0.8 if anchor_pct >= 30 else 0.55 if anchor_pct >= 15 else 0.3
    if marquee:
        s = min(1.0, s + 0.15)
    return s


def score_anchor_flag(has_anchor: Optional[bool]) -> Optional[float]:
    """Used when the source only gives a yes/no anchor flag (investorgain's
    live GMP table), not an actual allocation percentage. Deliberately mild --
    a low-information signal, not a strong vote either way."""
    if has_anchor is None:
        return None
    return 0.6 if has_anchor else 0.4


def score_gmp(gmp_value, issue_price) -> Optional[float]:
    if gmp_value is None or not issue_price:
        return None
    pct = (gmp_value / issue_price) * 100
    if pct <= 0:
        return 0.15
    if pct < 10:
        return 0.4
    if pct < 30:
        return 0.65
    if pct < 60:
        return 0.82
    return 0.7  # extreme GMP is itself a caution flag, not pure green


def composite_rating(scores: Dict[str, Optional[float]]) -> Optional[float]:
    """Returns a 1-5 star rating from whichever component scores are present."""
    entered = {k: v for k, v in scores.items() if v is not None}
    if len(entered) < 2:
        return None
    total_weight = sum(WEIGHTS[k] for k in entered)
    composite = sum(v * WEIGHTS[k] for k, v in entered.items()) / total_weight
    return round(1 + composite * 4, 1)  # map 0..1 onto 1..5 stars


def template_verdict(scores: Dict[str, Optional[float]], stars: Optional[float]) -> str:
    if stars is None:
        return "Not enough data was found to form a read on this one."
    entered = {k: v for k, v in scores.items() if v is not None}
    strongest = max(entered, key=entered.get)
    weakest = min(entered, key=entered.get)
    tier = "leans positive" if stars >= 3.3 else "is mixed" if stars >= 2.3 else "leans weak"
    return (
        f"The overall read {tier}. {strongest.title()} is the strongest signal found, "
        f"{weakest.title()} the weakest. This is a mechanical read of the numbers found, "
        f"not a recommendation."
    )


def llm_verdict(company: str, facts: Dict[str, Any], stars: Optional[float]) -> Optional[str]:
    """Optional: ask Claude to write a natural-language verdict from the same
    facts the scorer used. Falls back to None on any failure so callers can
    use template_verdict instead."""
    if not LLM_ENABLED:
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    f"Write a 2-3 sentence, plain-language verdict for the IPO of "
                    f"'{company}' based on these facts: {facts}. The computed star "
                    f"rating is {stars}/5. Do not repeat raw numbers verbatim, "
                    f"just explain what's driving the read. Do not give buy/sell advice."
                ),
            }],
        )
        return "".join(block.text for block in message.content if block.type == "text").strip()
    except Exception:  # noqa: BLE001
        return None
