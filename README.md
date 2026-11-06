# Squeeze — penny stock watchlist

Live at https://squeeze.thisisfed.xyz

An auto-updating watchlist of sub-$5 US exchange-listed stocks (the kind tradable on Robinhood UK), each with a catalyst, the key risk, the next dated trigger and the SEC filings that signal dilution.

- **Hand-curated:** `data/watchlist.json` — tickers, tiers, notes, `next` catalyst dates, the calendar and the post-catalyst track record.
- **Automatic** (`scripts/update.py` → `data/live.json`, `og.png`):
  - delayed price, day / 5-day change, relative volume, 30-day price + volume series (Yahoo via `yfinance`)
  - market cap, float, short interest, days to cover, next earnings date where Yahoo has them
  - dilution-type SEC filings in the last 30 days (S-1, S-3, F-1, F-3, 424B, EFFECT), linked to EDGAR
  - a 0–10 setup score: catalyst proximity, float, short interest, relative volume, minus dilution filings and pump/collapse signs
  - a scan of the day's sub-$5 movers on US exchanges with red flags
  - a social preview image of the top setups
- **Personal layer:** stars, notes and filing alerts live in the visitor's browser (localStorage). No accounts.
- **Schedule:** GitHub Actions, every 30 minutes 09:00–23:30 UTC on weekdays plus a daily weekend pass, deployed to GitHub Pages.

Run locally: `pip install -r requirements.txt && python scripts/update.py` (or `--mock` offline), then `python -m http.server`.

Not investment advice.
