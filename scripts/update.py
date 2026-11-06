"""Refresh live data for the penny watchlist.

Reads data/watchlist.json (hand-curated) and writes data/live.json with:
  - price, day change, volume, relative volume, 52-week range, market cap per ticker (Yahoo Finance)
  - recent SEC filings that usually mean new shares are coming (S-1, S-3, F-1, F-3, 424B...)
  - a scan of today's big sub-$5 movers on US exchanges, with automatic red flags

Free data only. Run: python scripts/update.py   (or --mock to test offline)
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WATCHLIST = ROOT / "data" / "watchlist.json"
OUT = ROOT / "data" / "live.json"

# SEC requires automated clients to send a contact email in the User-Agent.
# Set it as the repository secret SEC_CONTACT (Settings → Secrets → Actions); without it, filings are skipped.
SEC_CONTACT = os.environ.get("SEC_CONTACT", "").strip()
SEC_UA = f"penny-watchlist {SEC_CONTACT}"
DILUTION_FORMS = ("S-1", "S-3", "F-1", "F-3", "424B", "S-1/A", "S-3/A", "F-1/A", "F-3/A", "EFFECT")
FILING_LOOKBACK_DAYS = 30
US_EXCHANGES = ["NMS", "NGM", "NCM", "NYQ", "ASE", "PCX"]  # Nasdaq tiers, NYSE, NYSE American, Arca


def clean(v):
    """Make numbers JSON-safe (no NaN/inf)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    if math.isnan(f) or math.isinf(f):
        return None
    return f


# ---------------------------------------------------------------- prices
def fetch_prices(tickers: list[str], errors: list[str]) -> dict:
    import yfinance as yf

    out: dict[str, dict] = {}
    hist = None
    try:
        hist = yf.download(
            tickers, period="3mo", interval="1d", group_by="ticker",
            auto_adjust=False, progress=False, threads=False,
        )
    except Exception as e:  # noqa: BLE001
        errors.append(f"history download failed: {e}")

    for t in tickers:
        row: dict = {}
        try:
            df = hist[t] if hist is not None and len(tickers) > 1 else hist
            df = df.dropna(subset=["Close"]) if df is not None else None
            if df is not None and len(df):
                closes = df["Close"]
                vols = df["Volume"]
                row["close"] = clean(closes.iloc[-1])
                row["prev_close"] = clean(closes.iloc[-2]) if len(closes) > 1 else None
                row["volume"] = clean(vols.iloc[-1])
                row["avg_vol_20d"] = clean(vols.iloc[-21:-1].mean()) if len(vols) > 2 else None
                row["chg_5d"] = clean((closes.iloc[-1] / closes.iloc[-6] - 1) * 100) if len(closes) > 5 else None
                row["chg_1m"] = clean((closes.iloc[-1] / closes.iloc[-22] - 1) * 100) if len(closes) > 21 else None
                row["spark"] = [clean(round(float(c), 4)) for c in closes.iloc[-30:]]
                row["as_of"] = df.index[-1].strftime("%Y-%m-%d")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{t}: history parse failed: {e}")

        try:
            fi = yf.Ticker(t).fast_info

            def grab(attr):
                try:
                    return getattr(fi, attr)
                except Exception:  # noqa: BLE001 — a single missing field shouldn't drop the rest
                    return None

            row["price"] = clean(grab("last_price"))
            row["prev_close"] = row.get("prev_close") or clean(grab("previous_close"))
            row["market_cap"] = clean(grab("market_cap"))
            row["year_high"] = clean(grab("year_high"))
            row["year_low"] = clean(grab("year_low"))
            row["shares"] = clean(grab("shares"))
            row["exchange"] = grab("exchange")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{t}: quote failed: {e}")

        price = row.get("price") or row.get("close")
        row["price"] = price
        if price and row.get("prev_close"):
            row["chg_pct"] = clean((price / row["prev_close"] - 1) * 100)
        if row.get("volume") and row.get("avg_vol_20d"):
            row["rel_vol"] = clean(row["volume"] / row["avg_vol_20d"])
        out[t] = row
    return out


# ---------------------------------------------------------------- SEC filings
def fetch_filings(tickers: list[str], errors: list[str]) -> dict:
    import requests

    if not SEC_CONTACT:
        errors.append("SEC filings skipped: add a SEC_CONTACT repository secret (an email) to enable them")
        return {}
    s = requests.Session()
    s.headers.update({"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"})
    try:
        r0 = s.get("https://www.sec.gov/files/company_tickers.json", timeout=30)
        r0.raise_for_status()
        m = r0.json()
        cik_by_ticker = {v["ticker"].upper(): int(v["cik_str"]) for v in m.values()}
    except Exception as e:  # noqa: BLE001
        errors.append(f"SEC ticker map failed: {e}")
        return {}

    cutoff = (datetime.now(timezone.utc) - timedelta(days=FILING_LOOKBACK_DAYS)).date().isoformat()
    out: dict[str, list] = {}
    for t in tickers:
        cik = cik_by_ticker.get(t.upper())
        if not cik:
            continue
        try:
            time.sleep(0.15)  # stay well under SEC's 10 requests/second
            j = s.get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", timeout=30).json()
            r = j["filings"]["recent"]
            hits = []
            for form, date, acc, doc in zip(r["form"], r["filingDate"], r["accessionNumber"], r["primaryDocument"]):
                if date < cutoff:
                    break
                if form.startswith(DILUTION_FORMS):
                    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}"
                    hits.append({"form": form, "date": date, "url": url})
            out[t] = hits[:6]
        except Exception as e:  # noqa: BLE001
            errors.append(f"{t}: SEC filings failed: {e}")
    return out


# ---------------------------------------------------------------- movers scan
def red_flags(q: dict) -> list[str]:
    flags = []
    price = q.get("price") or 0
    hi = q.get("year_high") or 0
    cap = q.get("market_cap") or 0
    if price and hi and hi / price > 50:
        flags.append("52w high >50× price (serial splits/collapse)")
    if cap and cap < 5_000_000:
        flags.append("market cap under $5M")
    if price and price < 0.10:
        flags.append("under $0.10 (delisting zone)")
    return flags


def scan_movers(errors: list[str]) -> list[dict]:
    import yfinance as yf

    quotes = []
    try:
        from yfinance import EquityQuery as Q

        query = Q("and", [
            Q("lt", ["intradayprice", 5]),
            Q("gt", ["intradayprice", 0.01]),
            Q("gt", ["percentchange", 15]),
            Q("gt", ["dayvolume", 1_000_000]),
            Q("eq", ["region", "us"]),
            Q("is-in", ["exchange", *US_EXCHANGES]),
        ])
        res = yf.screen(query, sortField="percentchange", sortAsc=False, size=40)
        quotes = res.get("quotes", [])
    except Exception as e:  # noqa: BLE001
        errors.append(f"custom screen failed, falling back: {e}")
        for name in ("day_gainers", "small_cap_gainers", "most_actives"):
            try:
                quotes += yf.screen(name, count=100).get("quotes", [])
            except Exception as e2:  # noqa: BLE001
                errors.append(f"screen {name} failed: {e2}")

    seen, movers = set(), []
    for q in quotes:
        sym = q.get("symbol")
        price = clean(q.get("regularMarketPrice"))
        chg = clean(q.get("regularMarketChangePercent"))
        if not sym or sym in seen or price is None or price >= 5 or (chg or 0) < 10:
            continue
        if q.get("exchange") and q["exchange"] not in US_EXCHANGES:
            continue
        seen.add(sym)
        vol = clean(q.get("regularMarketVolume"))
        avg = clean(q.get("averageDailyVolume3Month"))
        m = {
            "ticker": sym,
            "name": q.get("shortName") or q.get("longName") or "",
            "price": price,
            "chg_pct": chg,
            "volume": vol,
            "rel_vol": clean(vol / avg) if vol and avg else None,
            "market_cap": clean(q.get("marketCap")),
            "year_high": clean(q.get("fiftyTwoWeekHigh")),
            "exchange": q.get("exchange"),
        }
        m["flags"] = red_flags(m)
        movers.append(m)
    movers.sort(key=lambda x: x["chg_pct"] or 0, reverse=True)
    return movers[:30]


# ---------------------------------------------------------------- mock (offline test)
def mock(tickers):
    import random
    random.seed(1)
    prices = {}
    for t in tickers:
        p = round(random.uniform(0.05, 4.5), 3)
        prices[t] = {"price": p, "prev_close": p / 1.05, "chg_pct": 5.0, "volume": 3e6, "avg_vol_20d": 1e6,
                     "rel_vol": 3.0, "chg_5d": -4.2, "chg_1m": 12.0, "market_cap": 2.4e7, "year_high": p * 8,
                     "year_low": p * 0.7, "spark": [p * (1 + random.uniform(-.2, .2)) for _ in range(30)],
                     "as_of": "2026-11-06"}
    filings = {"RCT": [{"form": "424B5", "date": "2026-11-04", "url": "https://www.sec.gov/"}]}
    movers = [{"ticker": "ABCD", "name": "Mock Mover Inc", "price": 0.42, "chg_pct": 88.0, "volume": 9e7,
               "rel_vol": 40.0, "market_cap": 3e6, "year_high": 120.0, "exchange": "NCM"}]
    for m in movers:
        m["flags"] = red_flags(m)
    return prices, filings, movers


def main():
    wl = json.loads(WATCHLIST.read_text())
    tickers = [s["ticker"] for s in wl["stocks"]]
    errors: list[str] = []
    if "--mock" in sys.argv:
        prices, filings, movers = mock(tickers)
    else:
        prices = fetch_prices(tickers, errors)
        filings = fetch_filings(tickers, errors)
        movers = scan_movers(errors)
    live = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "Yahoo Finance (delayed) · SEC EDGAR",
        "stocks": prices,
        "filings": filings,
        "movers": movers,
        "errors": errors[:50],
    }
    OUT.write_text(json.dumps(live, indent=1, allow_nan=False))
    ok = sum(1 for v in prices.values() if v.get("price"))
    print(f"wrote {OUT.name}: {ok}/{len(tickers)} priced, {len(movers)} movers, {len(errors)} errors")
    for e in errors[:10]:
        print("  !", e)
    for t, v in list(prices.items())[:8]:
        print(f"  {t:6} ${v.get('price')}  day {v.get('chg_pct') and round(v['chg_pct'], 1)}%  cap {v.get('market_cap')}")
    print("  filings:", {k: [f["form"] for f in v] for k, v in filings.items() if v})
    print("  movers:", [(m["ticker"], m["price"], round(m["chg_pct"] or 0)) for m in movers[:8]])
    if ok == 0 and "--mock" not in sys.argv:
        sys.exit("no prices fetched — keeping previous deploy")


if __name__ == "__main__":
    main()
