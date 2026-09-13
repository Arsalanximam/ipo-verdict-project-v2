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
│   ├── main.py         FastAPI app, all 7 routes, serves the frontend.
│   │                    Also currently holds the financial/valuation
│   │                    scraping logic (fetch_financial_intelligence) —
│   │                    that belongs in scrapers/, not here. Known
│   │                    cleanup item, see "Honest limitations" below.
│   ├── service.py       cache-or-refresh orchestration (the one code
│   │                    path both the API and the scheduler call into)
│   ├── analyzer.py      transparent weighted scoring + optional LLM verdict
│   ├── scheduler.py      background job that keeps a watchlist warm
│   ├── models.py         SQLAlchemy models (one row per snapshot)
│   ├── schemas.py        Pydantic request/response schemas
│   ├── database.py       SQLite engine/session setup
│   ├── config.py         env var loading, all with safe defaults
│   └── scrapers/
│       ├── nse.py         NSE subscription data (session/cookie warm-up pattern)
│       └── gmp.py         GMP via Playwright (the tracker page is JS-rendered)
├── frontend/
│   └── index.html       single-page dashboard (HTML/CSS/JS in one file),
│                          calls the API above
├── requirements.txt
├── Dockerfile
└── .env.example
```

## Design decisions

- **Scheduled background scraping, not live-per-request.** `scheduler.py`
  keeps a watchlist warm on a fixed interval (`REFRESH_INTERVAL_MINUTES`),
  and every request just reads from cache unless it's stale (`CACHE_TTL_MINUTES`)
  or `?force=true` is passed. This keeps normal page loads fast and
  avoids hammering NSE/GMP sources on every visitor — the cost of a slow
  scrape is paid once in the background, not by whoever happens to load
  the page next.
- **One shared cache-or-refresh function.** Both the scheduler and the
  API call the same function in `service.py` instead of each having
  their own copy of "check cache, scrape if stale" logic — so there's
  exactly one place that logic can go wrong, not two that can drift
  apart.
- **GMP is deliberately the lowest-weighted signal** in the scoring
  formula (`analyzer.py`), even though it's the number most retail
  investors fixate on — because it's unofficial and unregulated,
  while subscription numbers and financials come from exchange/company
  filings. The weighting is explicit and adjustable, not hidden inside
  a black-box model.
- **LLM verdict is optional, with a template fallback.** If
  `ANTHROPIC_API_KEY` isn't set, `analyzer.py` still produces a
  rule-based verdict paragraph from the same score breakdown — the
  app never depends on the LLM call succeeding.

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
- `GET /api/ipo/{company}/financials` — financial + valuation intelligence,
  scraped from IPOStation's offer-document review pages.
- `GET /api/ipo/{company}/history` — full snapshot history for one company
  (every GMP/subscription reading ever recorded for it).
- `GET /api/ipos` — everything currently cached, most recent first.
- `GET /api/search?q=` — searches the *entire* snapshot history, not just
  the current dashboard window, so closed/delisted IPOs are still findable.
- `GET /api/dashboard` — the aggregated view the frontend loads on open:
  market overview counts, current-GMP table, GMP/subscription leaders.
- `GET /api/gmp-weekly` — market-wide GMP intelligence for the current
  Monday–Sunday week (averages, medians, biggest weekly movers).
- `GET /` — serves the frontend itself.

There is no `/api/health` yet — it's a natural next addition once the
scrapers are split out of `main.py` (see below), since a real health
check should report scraper success rate and last-successful-scrape
time, not just "the process is running."

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
- **Financials and valuation data are scraped from IPOStation's**
  offer-document review pages (`/api/ipo/{company}/financials`) rather
  than parsed directly from each company's RHP PDF — RHPs don't have a
  stable structure across companies, so this uses IPOStation's
  already-normalized version instead. When that lookup fails, the
  scorer treats the data as "not available" and automatically
  reweights around whatever *was* found rather than guessing. A
  from-scratch RHP-PDF parser is a natural next step if IPOStation's
  coverage is ever incomplete.
- **`fetch_financial_intelligence` currently lives in `main.py`**
  instead of `scrapers/`, unlike every other scraper. It works fine,
  it's just in the wrong file — a leftover from adding the feature
  quickly. Splitting it out is next on the list.
- **Scraper selectors are inherently fragile.** NSE and the GMP tracker
  can change their markup at any time without notice, which would break
  the relevant scraper until selectors are updated. Both scrapers are
  written to fail gracefully (log a warning, return `None`/empty)
  rather than crash the app when that happens — but there's no
  automated alerting yet if a source silently goes stale for days.
  A `/api/health` endpoint reporting last-successful-scrape time per
  source (see above) would close that gap.

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
