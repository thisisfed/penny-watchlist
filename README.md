# Squeeze — penny stock watchlist

Live at https://squeeze.thisisfed.xyz

An auto-updating watchlist of sub-$5 US exchange-listed stocks (the kind tradable on Robinhood UK), each with a catalyst, the key risk, the next dated trigger and the SEC filings that signal dilution.

- **Hand-curated:** `data/watchlist.json` — tickers, tiers, notes, `next` catalyst dates, the calendar and the post-catalyst track record.
- **Automatic** (`scripts/update.py` → `data/live.json`, `og.png`):
  - delayed price, day / 5-day change, relative volume, 30-day price + volume series (Yahoo via `yfinance`)
  - market cap, float, short interest, days to cover, next earnings date where Yahoo has them
  - dilution-type SEC filings in the last 30 days (S-1, S-3, F-1, F-3, 424B, EFFECT), linked to EDGAR
  - FINRA daily short-sale volume (Reg SHO files, off-exchange trades)
  - the **squeeze meter** (`scripts/squeeze.py`, tested in `tests/`): 0–10 with a confidence grade
    - fuel: short interest (3), days to cover (1.5), float (1.5)
    - spark: shorts under water on rising volume (2), short-sale volume (1), dated catalyst (1) — scaled down when few shares are short
    - brakes: dilution filings (to −3), collapse signs (to −1.5); avoid-tier names are capped at 2
    - hygiene: stale short data earns half credit, reverse splits after the short-interest date are adjusted for, disagreeing sources use the lower figure, intraday volume is projected to a full day, and a failed Yahoo fetch falls back to the last good values
  - the **pop score**: 0–10 for the chance of a +10% run within five sessions — the stock's own 3-month habit of doing so (4), recent daily swings (2), volume (2), catalyst within days (1.5), small float (0.5), minus fresh offering prospectuses
  - a self-audit (both scores): each session's scores are logged once to `data/state.json` (committed by the workflow) and graded against the next 5 sessions; the page shows the result, good or bad
  - a scan of the day's sub-$5 movers on US exchanges with red flags
  - a social preview image of the top setups
- **Personal layer:** stars, notes and filing alerts live in the visitor's browser (localStorage). No accounts.
- **Schedule:** GitHub Actions, every 30 minutes 09:00–23:30 UTC on weekdays plus a daily weekend pass, deployed to GitHub Pages.

Run locally: `pip install -r requirements.txt && python scripts/update.py` (or `--mock` offline), then `python -m http.server`. Tests: `python -m unittest discover tests`.

Not investment advice.
