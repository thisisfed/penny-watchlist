"""Refresh live data for the penny watchlist.

Reads data/watchlist.json (hand-curated) and writes data/live.json with:
  - price, day / 5-day / 1-month change, relative volume, 30-day price + volume series (Yahoo Finance)
  - market cap, float, short interest, days to cover, next earnings date (Yahoo, where available)
  - recent SEC filings that usually mean new shares are coming (S-1, S-3, F-1, F-3, 424B, EFFECT)
  - a 0-10 "setup strength" score with its breakdown
  - a scan of today's big sub-$5 movers on US exchanges, with automatic red flags
Then renders og.png (social preview) from the result.

Free data only. Run: python scripts/update.py   (or --mock to test offline)
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WATCHLIST = ROOT / "data" / "watchlist.json"
OUT = ROOT / "data" / "live.json"

# SEC requires automated clients to send a contact email in the User-Agent.
# Set it as the repository secret SEC_CONTACT; without it, filings are skipped.
SEC_CONTACT = os.environ.get("SEC_CONTACT", "").strip()
SEC_UA = f"penny-watchlist {SEC_CONTACT}"
DILUTION_FORMS = ("S-1", "S-3", "F-1", "F-3", "424B", "EFFECT")
FILING_LOOKBACK_DAYS = 30
US_EXCHANGES = ["NMS", "NGM", "NCM", "NYQ", "ASE", "PCX"]


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


def r4(v):
    v = clean(v)
    return None if v is None else round(v, 4)


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
                closes, vols = df["Close"], df["Volume"]
                row["close"] = clean(closes.iloc[-1])
                row["prev_close"] = clean(closes.iloc[-2]) if len(closes) > 1 else None
                row["volume"] = clean(vols.iloc[-1])
                row["avg_vol_20d"] = clean(vols.iloc[-21:-1].mean()) if len(vols) > 2 else None
                row["chg_5d"] = clean((closes.iloc[-1] / closes.iloc[-6] - 1) * 100) if len(closes) > 5 else None
                row["chg_1m"] = clean((closes.iloc[-1] / closes.iloc[-22] - 1) * 100) if len(closes) > 21 else None
                row["spark"] = [r4(c) for c in closes.iloc[-30:]]
                row["vspark"] = [clean(v) for v in vols.iloc[-30:]]
                row["as_of"] = df.index[-1].strftime("%Y-%m-%d")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{t}: history parse failed: {e}")

        tk = yf.Ticker(t)
        try:
            fi = tk.fast_info

            def grab(attr):
                try:
                    return getattr(fi, attr)
                except Exception:  # noqa: BLE001
                    return None

            row["price"] = clean(grab("last_price"))
            row["prev_close"] = row.get("prev_close") or clean(grab("previous_close"))
            row["market_cap"] = clean(grab("market_cap"))
            row["year_high"] = clean(grab("year_high"))
            row["year_low"] = clean(grab("year_low"))
            row["shares"] = clean(grab("shares"))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{t}: quote failed: {e}")

        # Float, short interest, earnings date: slower endpoint, best effort.
        try:
            info = tk.info or {}
            row["float_shares"] = clean(info.get("floatShares"))
            si = clean(info.get("shortPercentOfFloat"))
            row["short_pct"] = si * 100 if si is not None and si < 5 else si
            row["days_to_cover"] = clean(info.get("shortRatio"))
            sid = info.get("dateShortInterest")
            row["short_as_of"] = datetime.fromtimestamp(sid, timezone.utc).strftime("%Y-%m-%d") if sid else None
            ets = info.get("earningsTimestampStart") or info.get("earningsTimestamp")
            if ets:
                ed = datetime.fromtimestamp(ets, timezone.utc).date()
                if ed >= date.today() - timedelta(days=1):
                    row["earnings_date"] = ed.isoformat()
            row["year_high"] = row.get("year_high") or clean(info.get("fiftyTwoWeekHigh"))
            row["market_cap"] = row.get("market_cap") or clean(info.get("marketCap"))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{t}: info failed: {e}")
        time.sleep(0.2)

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
        cik_by_ticker = {v["ticker"].upper(): int(v["cik_str"]) for v in r0.json().values()}
    except Exception as e:  # noqa: BLE001
        errors.append(f"SEC ticker map failed: {e}")
        return {}

    cutoff = (datetime.now(timezone.utc) - timedelta(days=FILING_LOOKBACK_DAYS)).date().isoformat()
    out: dict[str, dict] = {}
    for t in tickers:
        cik = cik_by_ticker.get(t.upper())
        if not cik:
            continue
        try:
            time.sleep(0.15)  # stay well under SEC's 10 requests/second
            j = s.get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", timeout=30).json()
            r = j["filings"]["recent"]
            hits = []
            for form, fdate, acc, doc in zip(r["form"], r["filingDate"], r["accessionNumber"], r["primaryDocument"]):
                if fdate < cutoff:
                    break
                if form.startswith(DILUTION_FORMS):
                    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}"
                    hits.append({"form": form, "date": fdate, "url": url, "id": acc})
            out[t] = {"cik": cik, "items": hits[:8]}
        except Exception as e:  # noqa: BLE001
            errors.append(f"{t}: SEC filings failed: {e}")
    return out


# ---------------------------------------------------------------- setup score
def days_until(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return (date.fromisoformat(iso) - datetime.now(timezone.utc).date()).days
    except ValueError:
        return None


def setup_score(s: dict, q: dict, filings: list[dict]) -> dict:
    """0-10 'setup strength'. Rewards a near dated catalyst, tight float, crowded short and
    unusual volume; penalises fresh dilution filings and pump/collapse signatures.
    Describes how primed a setup is, not whether it's a good investment."""
    parts = []

    nxt = s.get("next") or q.get("earnings_date")
    d = days_until(nxt)
    cat = 0.0
    if d is not None and d >= 0:
        cat = 3 if d <= 3 else 2.5 if d <= 7 else 2 if d <= 14 else 1 if d <= 30 else 0
    parts.append(["Catalyst", cat, f"in {d} days" if d is not None and d >= 0 else "none dated"])

    fl = q.get("float_shares") or q.get("shares")
    fs = 0.0
    if fl:
        fs = 2.5 if fl < 2e6 else 2 if fl < 10e6 else 1.5 if fl < 30e6 else 0.75 if fl < 100e6 else 0
    parts.append(["Float", fs, f"{fl/1e6:.1f}M shares" if fl else "unknown"])

    si = q.get("short_pct")
    ss = 0.0
    if si is not None:
        ss = 1.5 if si >= 30 else 1 if si >= 15 else 0.5 if si >= 8 else 0
    parts.append(["Short interest", ss, f"{si:.0f}% of float" if si is not None else "n/a"])

    rv = q.get("rel_vol")
    rs = 0.0
    if rv is not None:
        rs = 2 if rv >= 5 else 1.25 if rv >= 2 else 0.5 if rv >= 1.2 else 0
    parts.append(["Rel. volume", rs, f"{rv:.1f}×" if rv is not None else "n/a"])

    dil = 0.0
    for f in filings:
        dil -= 1.0 if f["form"].startswith("424B") else 0.75
    dil = max(dil, -3.0)
    parts.append(["Dilution filings", dil, f"{len(filings)} in 30 days"])

    pump = 0.0
    price, hi, cap = q.get("price"), q.get("year_high"), q.get("market_cap")
    if price and hi and hi / price > 50:
        pump -= 1
    if cap and cap < 5e6:
        pump -= 0.5
    parts.append(["Pump / collapse signs", pump, "52w high >50× or cap <$5M" if pump else "none"])

    total = 1.0 + sum(p[1] for p in parts)  # small base so clean, undated names aren't all zero
    if s.get("tier") == "X":
        total = min(total, 2.0)
    total = max(0.0, min(10.0, total))
    label = s.get("next_label") if s.get("next") else ("Earnings (Yahoo est.)" if nxt else None)
    return {"score": round(total, 1), "parts": parts, "next_date": nxt, "next_label": label, "days_to_next": d}


# ---------------------------------------------------------------- movers scan
def red_flags(q: dict) -> list[str]:
    flags = []
    price, hi, cap = q.get("price") or 0, q.get("year_high") or 0, q.get("market_cap") or 0
    if price and hi and hi / price > 50:
        flags.append("52w high >50× price")
    if cap and cap < 5_000_000:
        flags.append("cap under $5M")
    if price and price < 0.10:
        flags.append("under $0.10")
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
        quotes = yf.screen(query, sortField="percentchange", sortAsc=False, size=40).get("quotes", [])
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
        vol, avg = clean(q.get("regularMarketVolume")), clean(q.get("averageDailyVolume3Month"))
        m = {
            "ticker": sym, "name": q.get("shortName") or q.get("longName") or "",
            "price": price, "chg_pct": chg, "volume": vol,
            "rel_vol": clean(vol / avg) if vol and avg else None,
            "market_cap": clean(q.get("marketCap")), "year_high": clean(q.get("fiftyTwoWeekHigh")),
            "exchange": q.get("exchange"),
        }
        m["flags"] = red_flags(m)
        movers.append(m)
    movers.sort(key=lambda x: x["chg_pct"] or 0, reverse=True)
    return movers[:30]


# ---------------------------------------------------------------- mock (offline test)
def mock(tickers):
    import random
    random.seed(7)
    prices = {}
    for i, t in enumerate(tickers):
        p = round(random.uniform(0.03, 4.5), 4)
        prices[t] = {"price": p, "prev_close": p / (1 + random.uniform(-.3, .4)), "volume": 3e6, "avg_vol_20d": 1e6,
                     "rel_vol": random.choice([0.4, 1.1, 2.5, 8.9]), "chg_5d": random.uniform(-35, 80),
                     "chg_1m": 12.0, "market_cap": random.choice([3e6, 2.4e7, 1.1e8]), "year_high": p * random.choice([2, 8, 60]),
                     "year_low": p * 0.7, "float_shares": random.choice([1.1e6, 8e6, 4e7, None]),
                     "short_pct": random.choice([None, 4.0, 18.0, 44.0]), "days_to_cover": 2.1,
                     "short_as_of": "2026-10-15", "spark": [r4(p * (1 + random.uniform(-.25, .25))) for _ in range(30)],
                     "vspark": [random.uniform(1e5, 9e6) for _ in range(30)], "as_of": "2026-11-06"}
        prices[t]["chg_pct"] = (p / prices[t]["prev_close"] - 1) * 100
    filings = {"RCT": {"cik": 2027360, "items": [{"form": "424B3", "date": "2026-10-19", "url": "https://www.sec.gov/", "id": "a1"},
                                                 {"form": "EFFECT", "date": "2026-10-29", "url": "https://www.sec.gov/", "id": "a2"}]},
               "INO": {"cik": 1055726, "items": [{"form": "424B5", "date": "2026-11-03", "url": "https://www.sec.gov/", "id": "a3"}]}}
    movers = [{"ticker": "ABCD", "name": "Mock Mover Inc", "price": 0.42, "chg_pct": 88.0, "volume": 9e7,
               "rel_vol": 40.0, "market_cap": 3e6, "year_high": 120.0, "exchange": "NCM"},
              {"ticker": "RCT", "name": "RedCloud Holdings", "price": 0.044, "chg_pct": 46.0, "volume": 9e7,
               "rel_vol": 9.0, "market_cap": 4.7e6, "year_high": 2.6, "exchange": "NCM"}]
    for m in movers:
        m["flags"] = red_flags(m)
    return prices, filings, movers


# ---------------------------------------------------------------- OG image
def render_og(wl: dict, live: dict, path: Path) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return
    W, H = 1200, 630
    bg, ink, mute, up, down, line = (18, 18, 20), (236, 236, 240), (150, 150, 160), (74, 222, 128), (248, 113, 113), (44, 44, 51)
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)

    def font(size, bold=False, mono=False):
        names = (["DejaVuSansMono-Bold.ttf" if bold else "DejaVuSansMono.ttf"] if mono else
                 ["DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"])
        for base in ("/usr/share/fonts/truetype/dejavu/", "/usr/share/fonts/dejavu/", ""):
            try:
                return ImageFont.truetype(base + names[0], size)
            except OSError:
                continue
        return ImageFont.load_default(size=size)

    d.text((60, 48), "squeeze.thisisfed.xyz", font=font(24, mono=True), fill=mute)
    d.text((60, 84), "Penny Stock Watchlist", font=font(54, bold=True), fill=ink)
    d.text((60, 152), "Sub-$5 setups, ranked by setup strength — with the dilution filings shown.", font=font(24), fill=mute)

    stocks = live["stocks"]
    names = {s["ticker"]: s for s in wl["stocks"]}
    ranked = sorted([t for t in stocks if names.get(t, {}).get("tier") != "X" and stocks[t].get("score") is not None],
                    key=lambda t: stocks[t]["score"], reverse=True)[:6]
    y = 212
    d.line((60, y - 14, W - 60, y - 14), fill=line, width=2)
    hdr = font(18, bold=True)
    for x, label in ((60, "TICKER"), (300, "PRICE"), (480, "DAY"), (640, "SCORE"), (800, "NEXT")):
        d.text((x, y), label, font=hdr, fill=mute)
    y += 40
    for t in ranked:
        q = stocks[t]
        d.text((60, y), t, font=font(34, bold=True, mono=True), fill=ink)
        p = q.get("price")
        d.text((300, y + 4), (f"${p:.4f}".rstrip("0").rstrip(".") if p and p < 1 else f"${p:.2f}") if p else "—", font=font(28), fill=ink)
        c = q.get("chg_pct")
        d.text((480, y + 4), f"{c:+.1f}%" if c is not None else "—", font=font(28, bold=True), fill=up if (c or 0) >= 0 else down)
        sc = q["score"]
        d.rounded_rectangle((640, y + 6, 640 + int(sc * 12), y + 30), radius=6, fill=up if sc >= 6 else (245, 158, 11) if sc >= 4 else mute)
        d.text((640 + int(sc * 12) + 10, y + 4), f"{sc:.1f}", font=font(24, bold=True), fill=ink)
        dn = q.get("days_to_next")
        d.text((800, y + 6), (f"in {dn}d · " if dn is not None and dn >= 0 else "") + (q.get("next_label") or "")[:26],
               font=font(22), fill=mute)
        y += 50
    upd = datetime.fromisoformat(live["updated_at"])
    d.text((60, H - 52), f"Updated {upd:%d %m %Y %H:%M} UTC · Yahoo (delayed) + SEC EDGAR · not investment advice",
           font=font(20), fill=mute)
    img.save(path, optimize=True)


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

    for s in wl["stocks"]:
        q = prices.setdefault(s["ticker"], {})
        items = (filings.get(s["ticker"]) or {}).get("items", [])
        q.update(setup_score(s, q, items))

    live = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "Yahoo Finance (delayed) · SEC EDGAR",
        "stocks": prices, "filings": filings, "movers": movers, "errors": errors[:50],
    }
    OUT.write_text(json.dumps(live, indent=1, allow_nan=False))
    try:
        render_og(wl, live, ROOT / "og.png")
    except Exception as e:  # noqa: BLE001
        print("  ! og image failed:", e)

    ok = sum(1 for v in prices.values() if v.get("price"))
    print(f"wrote {OUT.name}: {ok}/{len(tickers)} priced, {len(movers)} movers, {len(errors)} errors")
    for e in errors[:10]:
        print("  !", e)
    top = sorted(prices.items(), key=lambda kv: kv[1].get("score") or 0, reverse=True)[:8]
    for t, v in top:
        print(f"  {t:6} score {v.get('score')}  ${v.get('price')}  day {v.get('chg_pct') and round(v['chg_pct'], 1)}%  "
              f"float {v.get('float_shares')}  SI {v.get('short_pct')}  next {v.get('next_date')}")
    print("  filings:", {k: [f["form"] for f in v["items"]] for k, v in filings.items() if v.get("items")})
    print("  movers:", [(m["ticker"], m["price"], round(m["chg_pct"] or 0)) for m in movers[:8]])
    if ok == 0 and "--mock" not in sys.argv:
        sys.exit("no prices fetched — keeping previous deploy")


if __name__ == "__main__":
    main()
