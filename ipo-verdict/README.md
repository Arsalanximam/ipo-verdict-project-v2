# IPO Verdict

A small full-stack app that answers one question — "is this IPO worth
a look?" — by scraping live grey-market-premium and subscription data
and reducing it to a single, explainable star rating.

**Live pipeline:** company name → NSE subscription scrape + GMP
tracker scrape → weighted scoring → (optional) LLM-written verdict →
cached in SQLite → served over a REST API → rendered on a small
dashboard.

## Why this exists

GMP (Grey Market Premium) is the number every retail investor in India
checks before applying for an IPO — but it's genuinely unofficial,
unregulated, and volatile. This project treats it as *one signal among
several*, weighted below official data like exchange-reported
subscription numbers, and is upfront about that trade-off instead of
pretending GMP is more reliable than it is.

## Architecture

```
ipo-verdict/
├── app/
│   ├── main.py         FastAPI app, routes, serves the frontend
│   ├── service.py       cache-or-refresh orchestration (the one code
│   │                    path both the API and the scheduler call into)
│   ├── analyzer.py      transparent weighted scoring + optional LLM verdict
│   ├── scheduler.py      background job that keeps a watchlist warm
│   ├── models.py         SQLAlchemy models (one row per snapshot)
│   ├── database.py       SQLite engine/session setup
│   ├── config.py         env var loading, all with safe defaults
│   └── scrapers/
│       ├── nse.py         NSE subscription data (session/cookie warm-up pattern)
│       └── gmp.py         GMP via Playwright (the tracker page is JS-rendered)
├── frontend/
│   └── index.html       single-page dashboard, calls the API above
├── requirements.txt
├── Dockerfile
└── .env.example
```

## Running it locally

```bash
python -m venv venv && source venv/bin/activate     # optional but recommended
pip install -r requirements.txt
playwright install chromium                          # one-time, for GMP scraping

cp .env.example .env                                  # then edit if you want the LLM verdict

uvicorn app.main:app --reload
```

Open **http://127.0.0.1:8000** for the dashboard, or
**http://127.0.0.1:8000/docs** for the interactive Swagger API docs.

## Endpoints

- `GET /api/ipo/{company}` — the main one. Returns cached data if it's
  fresh (see `CACHE_TTL_MINUTES`), otherwise scrapes fresh and caches it.
  Pass `?force=true` to always re-scrape.
- `GET /api/ipos` — everything currently cached, most recent first.
- `GET /api/health` — liveness check.

## Honest limitations (worth saying out loud in an interview)

- **NSE has no documented public API.** This uses the same
  cookie-warm-up pattern community libraries use
  (`bennythadikaran/NseIndiaApi` is a good reference) — hit the
  homepage first to get session cookies, then call the internal JSON
  endpoint. NSE can change that endpoint or its anti-bot logic without
  notice; the code is written to log a warning and return `None`
  rather than crash when that happens.
- **The GMP tracker page is JS-rendered**, not static HTML — the raw
  page literally ships a "Loading..." placeholder and fills the table
  via a background request afterward. That's why this uses Playwright
  (a real headless browser) instead of `requests` + BeautifulSoup for
  that one scraper.
- **Financials, valuation, and anchor-investor data aren't scraped at
  all** in this version — that information lives inside each
  company's RHP, which is a per-company PDF, not a page with a stable
  structure. The scorer treats these as "not available" and
  automatically reweights around whatever *was* found. A natural next
  feature is an RHP-PDF parser.
- **This sandbox's network can't actually reach nseindia.com or
  investorgain.com** (both scrapers were tested and confirmed to fail
  *gracefully* — logged warnings, not crashes — rather than tested
  against live data). On a normal machine with open internet access,
  both should work, though scraper selectors may need small
  adjustments if either site's markup has changed.

## Deploying

Any host that runs a Dockerfile works (Render, Railway, Fly.io). Free
tier is enough for this.

1. Push this repo to GitHub.
2. On Render: New → Web Service → connect the repo → it'll detect the
   Dockerfile automatically.
3. Add your `ANTHROPIC_API_KEY` (optional) as an environment variable
   in the host's dashboard — never in the repo.
4. Swap `DATABASE_URL` for the Postgres URL the host gives you if you
   want the cache to survive redeploys (SQLite's file otherwise resets
   each deploy on most free tiers).

## Talking points for an interview

- Why the LLM key lives server-side only, and what breaks if it doesn't.
- The cookie-warm-up scraping pattern, and why it's necessary.
- Why GMP is deliberately the *lowest*-weighted signal, not the headline one.
- The cache-then-serve pattern (`service.py`) and why both the API and
  the scheduler go through the same function instead of duplicating logic.
- What you'd build next: RHP PDF parsing, a GMP trend chart from the
  snapshot history already being stored, WebSocket push instead of
  polling.
