"""Refresh live data for the penny watchlist.

Reads data/watchlist.json (hand-curated) and writes data/live.json with:
  - price, day / 5-day / 1-month change, relative volume, 30-day price + volume series (Yahoo Finance)
  - market cap, float, short interest, days to cover, next earnings date (Yahoo, where available)
  - recent SEC filings that usually mean new shares are coming (S-1, S-3, F-1, F-3, 424B, EFFECT)
  - FINRA daily short-sale volume (free Reg SHO files)
  - a 0-10 squeeze meter with a confidence grade and its breakdown (model in scripts/squeeze.py)
  - a 0-10 "pop" score: how likely a +10% run is within five sessions
  - a self-audit: each day's scores are logged to data/state.json and graded against the next 5 sessions
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
STATE = ROOT / "data" / "state.json"   # committed: last-good float/short data + the score log

sys.path.insert(0, str(Path(__file__).resolve().parent))
import squeeze  # noqa: E402

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
def history_row(df, now: datetime) -> dict:
    """Quote fields from one ticker's daily bars. Kept separate from the network code so it can be tested."""
    row: dict = {}
    df = df.dropna(subset=["Close"])
    if not len(df):
        return row
    closes, vols = df["Close"], df["Volume"]
    highs = df["High"] if "High" in df else closes
    bar_date = df.index[-1].date()
    row["close"] = clean(closes.iloc[-1])
    row["prev_close"] = clean(closes.iloc[-2]) if len(closes) > 1 else None
    row["volume"] = clean(vols.iloc[-1])
    row["avg_vol_20d"] = clean(vols.iloc[-21:-1].mean()) if len(vols) > 2 else None
    prev_avg = clean(vols.iloc[-22:-2].mean()) if len(vols) > 3 else None
    prev_rv = clean(vols.iloc[-2] / prev_avg) if prev_avg else None
    rv, basis = squeeze.live_rel_vol(row["volume"], row["avg_vol_20d"], bar_date, prev_rv, now)
    row["rel_vol"], row["rel_vol_basis"] = clean(rv), basis
    row["chg_5d"] = clean((closes.iloc[-1] / closes.iloc[-6] - 1) * 100) if len(closes) > 5 else None
    row["chg_1m"] = clean((closes.iloc[-1] / closes.iloc[-22] - 1) * 100) if len(closes) > 21 else None
    row["spark"] = [r4(c) for c in closes.iloc[-30:]]
    row["vspark"] = [clean(v) for v in vols.iloc[-30:]]
    row["as_of"] = bar_date.isoformat()
    row["_series"] = [(i.strftime("%Y-%m-%d"), clean(c), clean(h)) for i, c, h in zip(df.index, closes, highs)]
    if "Stock Splits" in df:
        sp = df["Stock Splits"].dropna()
        sp = sp[(sp > 0) & (sp != 1)]
        if len(sp):
            row["split"] = {"date": sp.index[-1].strftime("%Y-%m-%d"), "ratio": clean(sp.iloc[-1])}
    return row


def fetch_prices(tickers: list[str], errors: list[str]) -> dict:
    import yfinance as yf

    now = datetime.now(timezone.utc)
    out: dict[str, dict] = {}
    hist = None
    for attempt in range(2):  # Yahoo drops the odd request; one retry removes most blank runs
        try:
            hist = yf.download(
                tickers, period="3mo", interval="1d", group_by="ticker",
                auto_adjust=False, actions=True, progress=False, threads=False,
            )
            if hist is not None and len(hist):
                break
        except Exception as e:  # noqa: BLE001
            if attempt:
                errors.append(f"history download failed: {e}")
            time.sleep(3)

    for t in tickers:
        row: dict = {}
        try:
            df = hist[t] if hist is not None and len(tickers) > 1 else hist
            if df is not None:
                row = history_row(df, now)
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
        # When it fails, main() falls back to the last good values in data/state.json.
        try:
            info = tk.info or {}
            row["float_shares"] = clean(info.get("floatShares"))
            row["shares_short"] = clean(info.get("sharesShort"))
            si = clean(info.get("shortPercentOfFloat"))  # Yahoo sends a fraction (0.18 = 18%)
            row["short_pct"] = si * 100 if si is not None and si < 3 else si
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


# ---------------------------------------------------------------- FINRA short-sale volume
def fetch_short_volume(errors: list[str], want: int = 5) -> list[dict]:
    """Last few FINRA Reg SHO daily short-sale volume files (off-exchange trades, all US-listed symbols)."""
    import requests

    days, day, tried = [], datetime.now(squeeze.NY).date(), 0
    s = requests.Session()
    s.headers.update({"User-Agent": "penny-watchlist (github.com/thisisfed/penny-watchlist)"})
    while len(days) < want and tried < 12:
        tried += 1
        if day.weekday() < 5:
            try:
                r = s.get(f"https://cdn.finra.org/equity/regsho/daily/CNMSshvol{day:%Y%m%d}.txt", timeout=30)
                if r.status_code == 200:
                    parsed = squeeze.parse_regsho(r.text)
                    if len(parsed) > 1000:  # a real file lists thousands of symbols
                        days.append(parsed)
            except Exception as e:  # noqa: BLE001
                errors.append(f"FINRA short volume {day}: {e}")
        day -= timedelta(days=1)
    if not days:
        errors.append("FINRA short-sale volume unavailable this run")
    return days


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
    today = datetime.now(timezone.utc).date()
    bdays = []
    d = today
    while len(bdays) < 64:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            bdays.append(d.isoformat())
    bdays.reverse()
    si_date = bdays[-12]
    prices, shortvol = {}, {}
    for t in tickers:
        p = round(random.uniform(0.03, 4.5), 4)
        path = [p * (1 + random.uniform(-.35, .35)) for _ in bdays[:-1]] + [p]
        fl = random.choice([1.1e6, 8e6, 4e7, None])
        sp = random.choice([None, 4.0, 18.0, 44.0])
        prices[t] = {"price": p, "prev_close": p / (1 + random.uniform(-.3, .4)), "volume": 3e6, "avg_vol_20d": 1e6,
                     "rel_vol": random.choice([0.4, 1.1, 2.5, 8.9]), "rel_vol_basis": "full day", "chg_5d": random.uniform(-35, 80),
                     "chg_1m": 12.0, "market_cap": random.choice([3e6, 2.4e7, 1.1e8]), "year_high": p * random.choice([2, 8, 60]),
                     "year_low": p * 0.7, "float_shares": fl, "shares": 5e7,
                     "short_pct": sp, "shares_short": fl * sp / 100 if fl and sp else None,
                     "short_as_of": si_date if sp is not None else None,
                     "spark": [r4(c) for c in path[-30:]], "vspark": [random.uniform(1e5, 9e6) for _ in range(30)],
                     "as_of": bdays[-1], "_series": [(d, r4(c), r4(c * 1.08)) for d, c in zip(bdays, path)]}
        prices[t]["chg_pct"] = (p / prices[t]["prev_close"] - 1) * 100
        if random.random() > 0.2:
            shortvol[t] = (random.uniform(3e5, 7e5), 1e6)
    prices[tickers[0]]["split"] = {"date": bdays[-1], "ratio": 0.05}
    filings = {"RCT": {"cik": 2027360, "items": [{"form": "424B3", "date": "2026-10-19", "url": "https://www.sec.gov/", "id": "a1"},
                                                 {"form": "EFFECT", "date": "2026-10-29", "url": "https://www.sec.gov/", "id": "a2"}]},
               "INO": {"cik": 1055726, "items": [{"form": "424B5", "date": "2026-11-03", "url": "https://www.sec.gov/", "id": "a3"}]}}
    movers = [{"ticker": "ABCD", "name": "Mock Mover Inc", "price": 0.42, "chg_pct": 88.0, "volume": 9e7,
               "rel_vol": 40.0, "market_cap": 3e6, "year_high": 120.0, "exchange": "NCM"},
              {"ticker": "RCT", "name": "RedCloud Holdings", "price": 0.044, "chg_pct": 46.0, "volume": 9e7,
               "rel_vol": 9.0, "market_cap": 4.7e6, "year_high": 2.6, "exchange": "NCM"}]
    for m in movers:
        m["flags"] = red_flags(m)
    # a fake score log so the self-audit panel has something to show
    state = squeeze.new_state()
    for d in bdays[-30:-6]:
        squeeze.record_day(state, d, {t: [round(random.uniform(0, 9), 1), random.choice([45, 70, 90]), round(random.uniform(0, 9), 1)] for t in tickers})
    return prices, filings, movers, [shortvol] * 5, state


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
    d.text((60, 152), "Sub-$5 short-squeeze setups, scored 0–10 — with the dilution filings shown.", font=font(24), fill=mute)

    stocks = live["stocks"]
    names = {s["ticker"]: s for s in wl["stocks"]}
    ranked = sorted([t for t in stocks if names.get(t, {}).get("tier") != "X" and stocks[t].get("score") is not None],
                    key=lambda t: stocks[t]["score"], reverse=True)[:6]
    y = 212
    d.line((60, y - 14, W - 60, y - 14), fill=line, width=2)
    hdr = font(18, bold=True)
    for x, label in ((60, "TICKER"), (300, "PRICE"), (480, "DAY"), (640, "SQUEEZE"), (800, "NEXT")):
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


def load_state() -> dict:
    try:
        st = json.loads(STATE.read_text())
        st.setdefault("fundamentals", {})
        st.setdefault("days", [])
        return st
    except (OSError, ValueError):
        return squeeze.new_state()


def main():
    wl = json.loads(WATCHLIST.read_text())
    tickers = [s["ticker"] for s in wl["stocks"]]
    errors: list[str] = []
    is_mock = "--mock" in sys.argv
    now = datetime.now(timezone.utc)
    today, frac = squeeze.session_fraction(now)
    if is_mock:
        prices, filings, movers, shortvol, state = mock(tickers)
    else:
        state = load_state()
        prices = fetch_prices(tickers, errors)
        filings = fetch_filings(tickers, errors)
        shortvol = fetch_short_volume(errors)
        movers = scan_movers(errors)

    series = {}
    for s in wl["stocks"]:
        t = s["ticker"]
        q = prices.setdefault(t, {})
        series[t] = q.pop("_series", [])
        flags = []
        note = squeeze.use_cache(q, state["fundamentals"].get(t), today)
        if note:
            flags.append(note)
        elif q.get("float_shares") or q.get("short_pct") is not None:
            state["fundamentals"][t] = {k: q.get(k) for k in ("float_shares", "shares_short", "short_pct", "short_as_of")}
            state["fundamentals"][t]["saved"] = today.isoformat()
        q.update(squeeze.short_volume_summary(shortvol, t))
        flags += squeeze.normalise_short(q, today)
        q["ret_since_si"] = clean(squeeze.return_since(series[t], q.get("short_as_of"), q.get("price")))
        if q.get("rel_vol_basis") == "previous session":
            flags.append("Relative volume is from the previous session (too early in the day)")
        q["flags"] = flags
        items = (filings.get(t) or {}).get("items", [])
        q.update(squeeze.squeeze_score(s, q, items, today))
        q.update(squeeze.pop_score(s, q, items, series[t], today))

    ok = sum(1 for v in prices.values() if v.get("price"))
    if ok == 0 and not is_mock:
        sys.exit("no prices fetched — keeping previous deploy")

    # Self-audit: log each finished session once, then grade older logs against what happened next.
    dates = [v["as_of"] for v in prices.values() if v.get("as_of")]
    bar = max(set(dates), key=dates.count) if dates else None
    finished = bar and (bar < today.isoformat() or frac >= 1 and now.astimezone(squeeze.NY).hour * 60 + now.astimezone(squeeze.NY).minute >= 980)
    if finished and not is_mock:
        squeeze.record_day(state, bar, {t: [v["score"], v["conf"], v.get("pop") if v.get("pop_known") else None] for t, v in prices.items()
                                        if v.get("as_of") == bar and v.get("score") is not None})
    squeeze.settle(state, series)
    if not is_mock:
        STATE.write_text(json.dumps(state, separators=(",", ":"), allow_nan=False) + "\n")

    live = {
        "updated_at": now.isoformat(timespec="seconds"),
        "source": "Yahoo Finance (delayed) · FINRA · SEC EDGAR",
        "stocks": prices, "filings": filings, "movers": movers, "calibration": squeeze.calibration(state),
        "pop_calibration": squeeze.calibration(state, col=2, hit=squeeze.POP_HIT),
        "errors": errors[:50],
    }
    OUT.write_text(json.dumps(live, indent=1, allow_nan=False))
    try:
        render_og(wl, live, ROOT / "og.png")
    except Exception as e:  # noqa: BLE001
        print("  ! og image failed:", e)

    print(f"wrote {OUT.name}: {ok}/{len(tickers)} priced, {len(movers)} movers, {len(errors)} errors")
    for e in errors[:10]:
        print("  !", e)
    top = sorted(prices.items(), key=lambda kv: kv[1].get("score") or 0, reverse=True)[:8]
    for t, v in top:
        print(f"  {t:6} squeeze {v.get('score')} ({v.get('conf_label')})  ${v.get('price')}  "
              f"pop {v.get('pop')} ({v.get('pop_rate')}% habit)  SI {v.get('short_pct') and round(v['short_pct'], 1)}  float {v.get('float_shares')}  flags {v.get('flags')}")
    print("  filings:", {k: [f["form"] for f in v["items"]] for k, v in filings.items() if v.get("items")})
    print("  movers:", [(m["ticker"], m["price"], round(m["chg_pct"] or 0)) for m in movers[:8]])
    print("  audit:", live["calibration"])


if __name__ == "__main__":
    main()
