"""Run: python -m unittest discover tests"""
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import squeeze as sq  # noqa: E402

TODAY = date(2026, 11, 13)


def base(**kw):
    q = {"price": 1.0, "float_shares": 8e6, "shares": 10e6, "shares_short": 2.4e6, "short_pct": 30.0,
         "short_as_of": "2026-10-30", "avg_vol_20d": 1e6, "rel_vol": 1.0, "chg_pct": 0.0, "chg_5d": 0.0,
         "ret_since_si": 0.0, "short_vol_ratio": 50.0, "short_vol_days": 5, "market_cap": 5e7, "year_high": 3.0}
    q.update(kw)
    q["flags"] = sq.normalise_short(q, TODAY)
    return q


def score(q, s=None, filings=()):
    return sq.squeeze_score(s or {"tier": "C"}, q, list(filings), TODAY)


class Model(unittest.TestCase):
    def test_bounds_and_monotonic_in_short_interest(self):
        last = -1
        for si in (0, 5, 10, 20, 30, 40, 60):
            r = score(base(short_pct=si, shares_short=si / 100 * 8e6))
            self.assertTrue(0 <= r["score"] <= 10)
            self.assertGreaterEqual(r["score"], last)
            last = r["score"]

    def test_no_shorts_no_squeeze(self):
        hot = dict(ret_since_si=80.0, rel_vol=9.0, chg_pct=40.0, short_vol_ratio=50.0)
        crowded = score(base(**hot), {"tier": "A", "next": "2026-11-14"})
        empty = score(base(short_pct=2.0, shares_short=1.6e5, **hot), {"tier": "A", "next": "2026-11-14"})
        self.assertGreater(crowded["score"], 6)
        self.assertLess(empty["score"], 3.5)

    def test_volume_on_a_falling_price_earns_nothing(self):
        up = score(base(rel_vol=6.0, chg_pct=12.0))
        down = score(base(rel_vol=6.0, chg_pct=-12.0, chg_5d=-30.0))
        self.assertGreater(up["score"], down["score"])

    def test_missing_short_data_lowers_confidence_not_just_score(self):
        r = score(base(short_pct=None, shares_short=None, short_as_of=None))
        self.assertEqual(r["conf_label"], "Low" if r["conf"] < 50 else "Medium")
        self.assertLess(r["conf"], 80)
        self.assertIn("No short interest data", r["flags"])
        self.assertEqual(score(base())["conf_label"], "High")

    def test_stale_short_data_gets_half_credit(self):
        fresh, stale = score(base()), score(base(short_as_of="2026-08-01"))
        self.assertAlmostEqual(stale["parts"][0][1], fresh["parts"][0][1] / 2, places=1)
        self.assertTrue(any("days old" in f for f in stale["flags"]))

    def test_dilution_and_avoid_tier(self):
        f = [{"form": "424B5"}, {"form": "S-3"}]
        self.assertAlmostEqual(score(base())["score"] - score(base(), filings=f)["score"], 1.8, delta=0.11)
        self.assertLessEqual(score(base(short_pct=60.0, ret_since_si=90.0), {"tier": "X"})["score"], 2.0)

    def test_parts_add_up(self):
        r = score(base(ret_since_si=25.0, rel_vol=3.0, chg_pct=5.0))
        self.assertAlmostEqual(sum(p[1] for p in r["parts"]), r["score"], delta=0.06)


class Hygiene(unittest.TestCase):
    def test_reverse_split_after_short_date_rescales_shares_short(self):
        q = base(shares_short=20e6, float_shares=4e6, shares=5e6, short_pct=None,
                 split={"date": "2026-11-09", "ratio": 0.05})
        self.assertAlmostEqual(q["shares_short"], 1e6)
        self.assertAlmostEqual(q["short_pct"], 25.0)

    def test_pre_split_float_is_caught(self):
        q = base(float_shares=80e6, shares=5e6, shares_short=20e6, short_pct=25.0,
                 split={"date": "2026-11-09", "ratio": 0.05})
        self.assertAlmostEqual(q["float_shares"], 4e6)
        self.assertAlmostEqual(q["short_pct"], 25.0)

    def test_disagreeing_sources_use_the_lower_number(self):
        q = base(short_pct=60.0)  # shares short / float says 30%
        self.assertAlmostEqual(q["short_pct"], 30.0)
        self.assertTrue(any("disagree" in f for f in q["flags"]))

    def test_days_to_cover_recomputed(self):
        self.assertAlmostEqual(base()["days_to_cover"], 2.4)

    def test_cache_fallback_only_when_fetch_is_empty_and_recent(self):
        c = {"float_shares": 5e6, "short_pct": 20.0, "shares_short": 1e6, "short_as_of": "2026-10-30", "saved": "2026-11-10"}
        q = {}
        self.assertIn("last good fetch", sq.use_cache(q, c, TODAY))
        self.assertEqual(q["float_shares"], 5e6)
        self.assertIsNone(sq.use_cache({"float_shares": 1e6}, c, TODAY))
        self.assertIsNone(sq.use_cache({}, dict(c, saved="2026-08-01"), TODAY))


class Volume(unittest.TestCase):
    def at(self, h, m):  # New York is UTC-5 in November
        return datetime(2026, 11, 13, h + 5, m, tzinfo=timezone.utc)

    def test_completed_bar_is_used_as_is(self):
        self.assertEqual(sq.live_rel_vol(3e6, 1e6, date(2026, 11, 12), 0.5, self.at(8, 0)), (3.0, "full day"))
        self.assertEqual(sq.live_rel_vol(3e6, 1e6, TODAY, 0.5, self.at(17, 0)), (3.0, "full day"))

    def test_midday_bar_is_projected(self):
        rv, basis = sq.live_rel_vol(1e6, 1e6, TODAY, 0.5, self.at(12, 45))  # half the session gone
        self.assertEqual(basis, "projected")
        self.assertTrue(1.5 < rv < 1.8)

    def test_first_half_hour_falls_back(self):
        self.assertEqual(sq.live_rel_vol(5e5, 1e6, TODAY, 0.7, self.at(9, 45)), (0.7, "previous session"))

    def test_summer_time(self):
        d, frac = sq.session_fraction(datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc))  # 09:30 EDT
        self.assertEqual((d, frac), (date(2026, 7, 1), 0.0))


class Finra(unittest.TestCase):
    def test_parse_and_summarise(self):
        txt = ("Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n"
               "20261112|GPUS|600000.000000|10|1000000.000000|Q,N\n20261112|INO|100|0|400|Q\n2\n")
        day = sq.parse_regsho(txt)
        self.assertEqual(day["GPUS"], (600000.0, 1000000.0))
        s = sq.short_volume_summary([day, day, {}], "GPUS")
        self.assertEqual((round(s["short_vol_ratio"]), s["short_vol_days"]), (60, 2))
        self.assertEqual(sq.short_volume_summary([day], "ZZZZ"), {})


class Audit(unittest.TestCase):
    def test_log_settle_and_grade(self):
        st = sq.new_state()
        self.assertTrue(sq.record_day(st, "2026-11-02", {"AAA": [8.0, 90], "BBB": [2.0, 90]}))
        self.assertFalse(sq.record_day(st, "2026-11-02", {"AAA": [1.0, 90]}))  # never rewritten
        days = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09"]
        ser = {"AAA": [(d, c, c * 1.1) for d, c in zip(days, [1, 1.2, 1.5, 1.4, 1.3, 1.25])],
               "BBB": [(d, c, c) for d, c in zip(days, [1, .9, .9, .8, .8, .7])][:5]}  # BBB not matured
        self.assertEqual(sq.settle(st, ser), 1)
        self.assertEqual(st["days"][0]["o"], {"AAA": [65.0, 25.0]})
        cal = sq.calibration(st)
        self.assertEqual((cal["settled"], cal["buckets"][0]["n"], cal["buckets"][0]["hit"]), (1, 1, 100))
        self.assertIsNone(cal["buckets"][2]["hit"])


class Pop(unittest.TestCase):
    def series(self, step):
        # alternating +step% / flat closes; highs equal closes
        out, c = [], 1.0
        for i in range(60):
            c *= (1 + step / 100) if i % 5 == 0 else 1.0
            out.append((f"d{i:02d}", c, c))
        return out

    def test_base_rate(self):
        self.assertEqual(sq.pop_base_rate(self.series(12)), 100.0)   # a 12% jump every 5 sessions
        self.assertEqual(sq.pop_base_rate(self.series(2)), 0.0)
        self.assertIsNone(sq.pop_base_rate(self.series(12)[:15]))

    def test_volatile_names_outscore_quiet_ones(self):
        q = {"rel_vol": 3.0, "chg_pct": 4.0, "float_shares": 5e6}
        hot = sq.pop_score({"next": "2026-11-14"}, q, [], self.series(12), TODAY)
        calm = sq.pop_score({}, {"rel_vol": 0.8, "chg_pct": -1.0, "float_shares": 3e8}, [], self.series(2), TODAY)
        self.assertGreater(hot["pop"], 6.5)
        self.assertLess(calm["pop"], 2)
        self.assertAlmostEqual(sum(p[1] for p in hot["pop_parts"]), hot["pop"], delta=0.06)

    def test_fresh_offering_subtracts_and_unknown_history_is_marked(self):
        q = {"rel_vol": 3.0, "chg_pct": 4.0}
        f = [{"form": "424B5", "date": "2026-11-11"}, {"form": "424B5", "date": "2026-09-01"}]
        self.assertAlmostEqual(sq.pop_score({}, q, [], self.series(12), TODAY)["pop"] - sq.pop_score({}, q, f, self.series(12), TODAY)["pop"], 0.75, delta=0.11)
        self.assertFalse(sq.pop_score({}, q, [], [], TODAY)["pop_known"])

    def test_pop_audit_uses_its_own_column_and_threshold(self):
        st = sq.new_state()
        sq.record_day(st, "2026-11-02", {"AAA": [1.0, 90, 8.0], "OLD": [1.0, 90]})
        st["days"][0]["o"] = {"AAA": [12.0, 3.0], "OLD": [50.0, 1.0]}
        cal = sq.calibration(st, col=2, hit=sq.POP_HIT)
        self.assertEqual((cal["settled"], cal["buckets"][0]["n"], cal["buckets"][0]["hit"]), (1, 1, 100))
        self.assertEqual(sq.calibration(st)["settled"], 2)


class HistoryRow(unittest.TestCase):
    def test_bars_to_quote_fields(self):
        try:
            import pandas as pd
        except ImportError:
            self.skipTest("pandas not installed")
        import update
        idx = pd.bdate_range(end="2026-11-12", periods=40)
        df = pd.DataFrame({"Close": [1.0] * 39 + [1.5], "High": [1.1] * 39 + [1.6], "Volume": [1e6] * 39 + [4e6],
                           "Stock Splits": [0.0] * 35 + [0.05] + [0.0] * 4}, index=idx)
        df.loc[idx[10], "Close"] = float("nan")
        row = update.history_row(df, datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(row["as_of"], "2026-11-12")
        self.assertEqual((row["rel_vol"], row["rel_vol_basis"]), (4.0, "full day"))
        self.assertAlmostEqual(row["chg_5d"], 50.0)
        self.assertEqual(row["split"]["ratio"], 0.05)
        self.assertEqual(len(row["_series"]), 39)
        self.assertAlmostEqual(sq.return_since(row["_series"], "2026-10-30", 1.5), 50.0)


if __name__ == "__main__":
    unittest.main()
