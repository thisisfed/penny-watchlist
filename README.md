# Penny Stock Watchlist

An auto-updating watchlist of sub-$5 US exchange-listed stocks (the kind tradable on Robinhood UK), each with a catalyst, the key risk and the next trigger date.

- **Hand-curated:** `data/watchlist.json`: tickers, tiers, notes, calendar. Edit this to change the list.
- **Automatic:** `scripts/update.py` fills in `data/live.json` with:
  - delayed price, day/5-day change, relative volume, market cap, 52-week high, 30-day sparkline (Yahoo Finance via `yfinance`)
  - recent SEC filings that usually mean new shares are coming (S-1, S-3, F-1, F-3, 424B…), linked
  - a scan of the day's biggest sub-$5 movers on US exchanges with red flags (tiny market cap, 52-week high far above price, sub-$0.10)
- **Schedule:** GitHub Actions runs every 30 minutes, 09:00–23:30 UTC, Monday–Friday, then deploys to GitHub Pages. Free on a public repo.

Run locally: `pip install -r requirements.txt && python scripts/update.py`, then serve the folder (`python -m http.server`).

Live at https://squeeze.thisisfed.xyz

Not investment advice.
