"""The squeeze meter: scoring model, data hygiene and self-audit.

Pure functions only (no network), so everything here is unit-tested in tests/.

Design rules, in order of importance:
  1. No shorts, no squeeze. Momentum and catalysts only count in proportion to
     how crowded the short side is.
  2. Unknown is not zero. Every score carries a confidence grade built from
     which inputs were present and fresh; missing data lowers confidence
     instead of quietly lowering (or inflating) the score.
  3. Stale share counts are the classic way these numbers go wrong, so reverse
     splits after the short-interest date are detected and adjusted for.
  4. The meter grades itself: every day's scores are logged and compared with
     what the price did over the next five sessions.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from statistics import median
from zoneinfo import ZoneInfo

MODEL = 2
NY = ZoneInfo("America/New_York")
SI_FRESH_DAYS = 40      # FINRA short interest: twice monthly, published ~8 business days late
CACHE_MAX_DAYS = 45     # how long last-known float / short data may stand in for a failed fetch
HORIZON = 5             # sessions used to judge a score
HIT = 20.0              # % gain within HORIZON that counts as "it squeezed"
BUCKETS = [("High", 6.5, 10.01), ("Mid", 4.5, 6.5), ("Low", 0.0, 4.5)]


def ramp(x, lo, hi):
    """0 at lo, 1 at hi, linear between. None -> 0."""
    if x is None:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def _iso(d) -> date | None:
    if isinstance(d, date):
        return d
    try:
        return date.fromisoformat(str(d)[:10])
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- relative volume
def session_fraction(now: datetime) -> tuple[date, float]:
    """(New York date, share of the 09:30-16:00 session elapsed)."""
    ny = now.astimezone(NY)
    mins = ny.hour * 60 + ny.minute - 570
    return ny.date(), max(0.0, min(1.0, mins / 390))


def live_rel_vol(vol, avg, bar_date, prev_rel_vol, now: datetime):
    """Relative volume that is comparable at any time of day.

    A half-finished session compared with full-day averages always looks quiet,
    so a live bar is projected to a full day (volume is front-loaded, hence the
    0.7 exponent). In the first half hour the projection is noise, so the last
    completed session is used instead. Returns (rel_vol, basis).
    """
    prev = (prev_rel_vol, "previous session") if prev_rel_vol is not None else (None, None)
    if not vol or not avg:
        return prev
    today, frac = session_fraction(now)
    if bar_date != today or frac >= 1:
        return vol / avg, "full day"
    if frac < 0.08:
        return prev
    return vol / avg / frac ** 0.7, "projected"


# ---------------------------------------------------------------- short data hygiene
def normalise_short(q: dict, today: date) -> list[str]:
    """Reconcile float, shares short and short % in place. Returns warning flags."""
    flags: list[str] = []
    fl, sh, ss, si = q.get("float_shares"), q.get("shares"), q.get("shares_short"), q.get("short_pct")
    split, as_of = q.get("split") or {}, q.get("short_as_of")
    ratio = split.get("ratio")
    quality = None

    split_after = bool(ratio and ratio != 1 and as_of and split.get("date", "") > as_of)
    if split_after and ss:
        ss *= ratio
        flags.append(f"Split on {split['date']} came after the short data: share counts adjusted")
    if ratio and ratio < 1 and fl and sh and fl > sh * 1.2:
        fl *= ratio
        flags.append("Float was still pre-split: adjusted")

    calc = ss / fl * 100 if ss and fl else None
    if calc is not None and si is not None:
        if max(calc, si) / max(min(calc, si), 1e-9) > 1.5:
            si = min(calc, si)
            flags.append("Short % sources disagree: using the lower figure")
    elif si is None:
        si = calc
    if si is not None and si > 100:
        si = 100.0
        flags.append("Short % above 100 capped")

    if si is not None:
        d = _iso(as_of)
        age = (today - d).days if d else None
        if split_after and calc is None:
            quality = "presplit"
            flags.append("Short % predates a split")
        elif age is None or age > SI_FRESH_DAYS:
            quality = "stale"
            flags.append(f"Short data is {age} days old" if age is not None else "Short data is undated")
        else:
            quality = "fresh"
        q["short_age_days"] = age

    q["float_shares"], q["shares_short"], q["short_pct"], q["si_quality"] = fl, ss, si, quality
    avg = q.get("avg_vol_20d")
    if ss and avg:
        q["days_to_cover"] = ss / avg  # recomputed: Yahoo's ratio uses its own, older volume window
    return flags


def return_since(series, iso: str | None, price) -> float | None:
    """% move from the first close on/after a date to the current price."""
    if not series or not iso or not price:
        return None
    if iso < series[0][0]:
        return None
    for d, close, _ in series:
        if d >= iso and close:
            return (price / close - 1) * 100
    return None


# ---------------------------------------------------------------- the score
def days_until(iso, today: date):
    d = _iso(iso)
    return (d - today).days if d else None


def squeeze_score(s: dict, q: dict, filings: list[dict], today: date) -> dict:
    """0-10 squeeze meter with a confidence grade and a full breakdown.

    Fuel (how trapped shorts could get):   short interest 3 · days to cover 1.5 · float 1.5
    Spark (is it starting):                pressure on shorts 2 · short-sale volume 1 · catalyst 1
    Brakes:                                dilution filings up to -3 · pump/collapse signs up to -1.5
    """
    flags = list(q.get("flags") or [])
    parts: list[list] = []
    conf = 0

    # --- fuel
    si, quality = q.get("short_pct"), q.get("si_quality")
    si_pts = 3 * ramp(si, 5, 40)
    if quality in ("stale", "presplit"):
        si_pts *= 0.5
    if si is None:
        parts.append(["Short interest", 0.0, "unknown", 3])
        flags.append("No short interest data")
    else:
        age = q.get("short_age_days")
        note = f"{si:.0f}% of float" + (f", {age}d old" if age is not None else "") + \
               (" (half credit)" if quality != "fresh" else "")
        parts.append(["Short interest", si_pts, note, 3])
        conf += 40 if quality == "fresh" else 20

    dtc = q.get("days_to_cover")
    parts.append(["Days to cover", 1.5 * ramp(dtc, 1, 6), f"{dtc:.1f} days" if dtc is not None else "unknown", 1.5])
    if dtc is not None and q.get("shares_short"):
        conf += 10

    fl, est = q.get("float_shares"), False
    if not fl and q.get("shares"):
        fl, est = q["shares"], True
        flags.append("Float unknown: using shares outstanding")
    fl_pts = 1.5 * ramp(math.log10(100e6 / fl), 0, math.log10(50)) if fl else 0.0
    parts.append(["Float", fl_pts, (f"{fl / 1e6:.1f}M shares" + (" (est.)" if est else "")) if fl else "unknown", 1.5])
    conf += 0 if not fl else 7 if est else 15

    # --- spark, gated by how crowded the short side is
    gate = 0.5 if si is None else 0.35 + 0.65 * ramp(si, 5, 20)
    gate_note = f"×{gate:.2f} short gate"

    rs = q.get("ret_since_si")
    basis = "since short data" if rs is not None else "1 month"
    if rs is None:
        rs = q.get("chg_1m")
    rv, chg, chg5 = q.get("rel_vol"), q.get("chg_pct"), q.get("chg_5d")
    rising = (chg or 0) > 0 or (chg5 or 0) > 0
    vol_pts = 0.8 * ramp(math.log(rv), math.log(1.5), math.log(6)) if rv and rv > 0 and rising else 0.0
    pres = (1.2 * ramp(rs, 0, 60) + vol_pts) * gate
    bits = [f"{rs:+.0f}% {basis}" if rs is not None else "price trend unknown",
            (f"{rv:.1f}× volume" + ("" if rising else " on a falling price")) if rv is not None else "volume unknown",
            gate_note]
    parts.append(["Pressure on shorts", pres, ", ".join(bits), 2])
    conf += (10 if rs is not None else 0) + (10 if rv is not None else 0)

    svr, svd = q.get("short_vol_ratio"), q.get("short_vol_days") or 0
    if svr is None or svd < 3:
        parts.append(["Short-sale volume", 0.0, "unknown", 1])
    else:
        parts.append(["Short-sale volume", ramp(svr, 45, 65), f"{svr:.0f}% of off-exchange volume, {svd} days", 1])
        conf += 15

    nxt = s.get("next") or q.get("earnings_date")
    d = days_until(nxt, today)
    cat = 0.0
    if d is not None and d >= 0:
        cat = 1 if d <= 3 else 0.8 if d <= 7 else 0.5 if d <= 14 else 0.25 if d <= 30 else 0
    parts.append(["Catalyst", cat * gate, (f"in {d} days" if d is not None and d >= 0 else "none dated") + f", {gate_note}", 1])

    # --- brakes
    dil = max(sum(-1.0 if f["form"].startswith("424B") else -0.75 for f in filings), -3.0)
    parts.append(["Dilution filings", dil, f"{len(filings)} in 30 days", 0])

    pump = 0.0
    price, hi, cap = q.get("price"), q.get("year_high"), q.get("market_cap")
    if price and hi and hi / price > 50:
        pump -= 1
    if cap and cap < 5e6:
        pump -= 0.5
    parts.append(["Pump / collapse signs", pump, "52w high >50× or cap <$5M" if pump else "none", 0])

    total = sum(p[1] for p in parts)
    if s.get("tier") == "X":
        total = min(total, 2.0)
    total = max(0.0, min(10.0, total))
    for p in parts:
        p[1] = round(p[1], 2)

    label = s.get("next_label") if s.get("next") else ("Earnings (Yahoo est.)" if nxt else None)
    return {
        "score": round(total, 1), "conf": conf,
        "conf_label": "High" if conf >= 80 else "Medium" if conf >= 50 else "Low",
        "parts": parts, "flags": flags, "model": MODEL,
        "next_date": nxt, "next_label": label, "days_to_next": d,
    }


# ---------------------------------------------------------------- FINRA daily short volume
def parse_regsho(text: str) -> dict[str, tuple[float, float]]:
    """Parse a FINRA Reg SHO daily file: Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market."""
    out: dict[str, tuple[float, float]] = {}
    for line in text.splitlines():
        p = line.strip().split("|")
        if len(p) < 5 or p[0] == "Date":
            continue
        try:
            short, total = float(p[2]), float(p[4])
        except ValueError:
            continue
        if total > 0:
            out[p[1].upper()] = (short, total)
    return out


def short_volume_summary(days: list[dict], ticker: str) -> dict:
    """Volume-weighted short-sale share over the supplied daily files."""
    short = total = 0.0
    n = 0
    for day in days:
        if ticker in day:
            a, b = day[ticker]
            short, total, n = short + a, total + b, n + 1
    if not total:
        return {}
    return {"short_vol_ratio": short / total * 100, "short_vol_days": n}


# ---------------------------------------------------------------- self-audit
def new_state() -> dict:
    return {"fundamentals": {}, "days": []}


def record_day(state: dict, bar_date: str, scores: dict[str, list]) -> bool:
    """Log one completed session's scores. Never rewrites a day already logged."""
    if not scores or any(d["d"] == bar_date for d in state["days"]):
        return False
    state["days"].append({"d": bar_date, "m": MODEL, "s": scores, "o": {}})
    state["days"].sort(key=lambda d: d["d"])
    return True


def settle(state: dict, series_by_ticker: dict[str, list]) -> int:
    """Fill in what happened over the next HORIZON sessions for any logged score that has matured."""
    done = 0
    for day in state["days"]:
        for t in day["s"]:
            if t in day["o"]:
                continue
            ser = series_by_ticker.get(t) or []
            idx = next((i for i, r in enumerate(ser) if r[0] == day["d"]), None)
            if idx is None or len(ser) < idx + 1 + HORIZON or not ser[idx][1]:
                continue
            base, win = ser[idx][1], ser[idx + 1: idx + 1 + HORIZON]
            highs = [r[2] or r[1] for r in win if (r[2] or r[1])]
            if not highs or not win[-1][1]:
                continue
            day["o"][t] = [round((max(highs) / base - 1) * 100, 1), round((win[-1][1] / base - 1) * 100, 1)]
            done += 1
    return done


def calibration(state: dict) -> dict:
    """How scores from this model version have actually played out."""
    rows = [(sc[0], day["o"][t]) for day in state["days"] if day.get("m") == MODEL
            for t, sc in day["s"].items() if t in day["o"]]
    logged = [d for d in state["days"] if d.get("m") == MODEL]
    buckets = []
    for label, lo, hi in BUCKETS:
        got = [o for s, o in rows if lo <= s < hi]
        buckets.append({
            "label": label, "lo": lo, "n": len(got),
            "hit": round(sum(1 for o in got if o[0] >= HIT) / len(got) * 100) if got else None,
            "med_max": round(median(o[0] for o in got), 1) if got else None,
            "med_ret": round(median(o[1] for o in got), 1) if got else None,
        })
    return {"model": MODEL, "horizon": HORIZON, "hit_pct": HIT, "settled": len(rows),
            "days_logged": len(logged), "since": logged[0]["d"] if logged else None, "buckets": buckets}


def use_cache(q: dict, cached: dict | None, today: date) -> str | None:
    """Fill float / short fields from the last good fetch when Yahoo returned nothing."""
    keys = ("float_shares", "shares_short", "short_pct", "short_as_of")
    if q.get("float_shares") or q.get("short_pct") is not None or q.get("shares_short"):
        return None
    saved = _iso((cached or {}).get("saved"))
    if not saved or (today - saved).days > CACHE_MAX_DAYS:
        return None
    for k in keys:
        q[k] = cached.get(k)
    return f"Float and short data from the last good fetch ({saved.isoformat()})"
