from __future__ import annotations

import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from bots.orb_m15 import _find_opening_range_candle, _session_start_for_candle_day_utc
from shared.time_utils import granularity_to_seconds, seconds_until_next_close
from shared.strategy_utils import candles_to_df, parse_order_ids, parse_realized_pl, pip_buffer_for_instrument


def _make_m15_bars(n: int, base_price: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range("2024-01-15 00:00", periods=n, freq="15min", tz="UTC")
    prices = [base_price + i * 0.01 for i in range(n)]
    return pd.DataFrame(
        {"open": prices, "high": [p + 0.5 for p in prices],
         "low": [p - 0.5 for p in prices], "close": prices},
        index=idx,
    )


class TestSessionStartForCandleDay(unittest.TestCase):
    def test_london_8am_maps_to_correct_utc_in_winter(self):
        # In winter London = UTC, so 08:00 London = 08:00 UTC
        candle_utc = pd.Timestamp("2024-01-15 10:00:00", tz="UTC")
        result = _session_start_for_candle_day_utc(
            candle_utc=candle_utc, session_tz="Europe/London", start_hhmm="08:00"
        )
        self.assertEqual(result.hour, 8)
        self.assertEqual(result.tzinfo, timezone.utc)

    def test_london_8am_maps_to_correct_utc_in_summer(self):
        # In summer London = UTC+1, so 08:00 London = 07:00 UTC
        candle_utc = pd.Timestamp("2024-07-15 10:00:00", tz="UTC")
        result = _session_start_for_candle_day_utc(
            candle_utc=candle_utc, session_tz="Europe/London", start_hhmm="08:00"
        )
        self.assertEqual(result.hour, 7)

    def test_new_york_930am_maps_to_utc(self):
        # In winter NY = UTC-5, so 09:30 NY = 14:30 UTC
        candle_utc = pd.Timestamp("2024-01-15 15:00:00", tz="UTC")
        result = _session_start_for_candle_day_utc(
            candle_utc=candle_utc, session_tz="America/New_York", start_hhmm="09:30"
        )
        self.assertEqual(result.hour, 14)
        self.assertEqual(result.minute, 30)


class TestFindOpeningRangeCandle(unittest.TestCase):
    def _bars(self):
        return _make_m15_bars(50)

    def test_finds_first_candle_at_or_after_session_start(self):
        bars = self._bars()  # starts 2024-01-15 00:00 UTC
        session_start = datetime(2024, 1, 15, 8, 0, tzinfo=timezone.utc)
        result = _find_opening_range_candle(bars, session_start_utc=session_start)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result, pd.Timestamp(session_start))

    def test_returns_none_when_no_candles_after_session(self):
        bars = _make_m15_bars(4)  # only 4 bars starting 00:00, all before 8am
        session_start = datetime(2024, 1, 15, 8, 0, tzinfo=timezone.utc)
        result = _find_opening_range_candle(bars, session_start_utc=session_start)
        self.assertIsNone(result)

    def test_returns_exact_match_at_session_start(self):
        bars = _make_m15_bars(50)
        # Bar at exactly 08:00
        session_start = datetime(2024, 1, 15, 8, 0, tzinfo=timezone.utc)
        result = _find_opening_range_candle(bars, session_start_utc=session_start)
        self.assertEqual(result, pd.Timestamp("2024-01-15 08:00:00", tz="UTC"))


class TestOrbBreakoutLogic(unittest.TestCase):
    """Test the breakout trigger conditions as inline logic (mirrors orb_m15.main)."""

    def test_long_breakout_detected(self):
        or_high, or_low = 100.5, 99.5
        buffer = 0.0
        last_close = 100.6  # above or_high
        self.assertTrue(last_close > (or_high + buffer))
        self.assertFalse(last_close < (or_low - buffer))

    def test_short_breakout_detected(self):
        or_high, or_low = 100.5, 99.5
        buffer = 0.0
        last_close = 99.4  # below or_low
        self.assertFalse(last_close > (or_high + buffer))
        self.assertTrue(last_close < (or_low - buffer))

    def test_no_breakout_within_range(self):
        or_high, or_low = 100.5, 99.5
        buffer = 0.0
        last_close = 100.0  # inside OR
        self.assertFalse(last_close > (or_high + buffer))
        self.assertFalse(last_close < (or_low - buffer))

    def test_buffer_prevents_marginal_breakout(self):
        or_high, or_low = 100.5, 99.5
        buffer = 0.2
        last_close = 100.6  # above or_high but not or_high + buffer
        self.assertFalse(last_close > (or_high + buffer))

    def test_r_mult_tp_calculation(self):
        entry_px = 100.0
        stop_px = 99.0
        r_mult = 2.0
        direction = 1
        r0 = abs(entry_px - stop_px)
        tp = entry_px + direction * (r_mult * r0)
        self.assertAlmostEqual(tp, 102.0)

    def test_limit_mode_symmetric_sl_tp(self):
        or_high = 100.5
        tp_sl_mult = 2.0
        direction = 1
        entry_px = or_high
        dist = tp_sl_mult * (or_high - 99.5)  # tp_sl_mult * R
        stop_px = entry_px - direction * dist
        tp_px = entry_px + direction * dist
        self.assertAlmostEqual(tp_px - entry_px, entry_px - stop_px)


class TestCandlesToDf(unittest.TestCase):
    def _make_candle(self, time: str, o=1.0, h=1.1, l=0.9, c=1.0, complete=True):
        return {"complete": complete, "time": time, "mid": {"o": str(o), "h": str(h), "l": str(l), "c": str(c)}}

    def test_builds_dataframe_with_utc_index(self):
        raw = {"candles": [self._make_candle("2024-01-01T00:00:00.000000000Z")]}
        df = candles_to_df(raw)
        self.assertEqual(len(df), 1)
        self.assertIsNotNone(df.index.tz)

    def test_skips_incomplete_candles(self):
        raw = {"candles": [
            self._make_candle("2024-01-01T00:00:00Z", complete=True),
            self._make_candle("2024-01-01T01:00:00Z", complete=False),
        ]}
        df = candles_to_df(raw)
        self.assertEqual(len(df), 1)

    def test_raises_on_all_incomplete(self):
        raw = {"candles": [self._make_candle("2024-01-01T00:00:00Z", complete=False)]}
        with self.assertRaises(RuntimeError):
            candles_to_df(raw)

    def test_ohlc_columns_present(self):
        raw = {"candles": [self._make_candle("2024-01-01T00:00:00Z", o=1.1, h=1.2, l=1.0, c=1.15)]}
        df = candles_to_df(raw)
        for col in ("open", "high", "low", "close"):
            self.assertIn(col, df.columns)
        self.assertAlmostEqual(df["high"].iloc[0], 1.2)


class TestParseOrderIds(unittest.TestCase):
    def test_market_order_fill(self):
        resp = {
            "orderCreateTransaction": {"id": "100"},
            "orderFillTransaction": {"orderID": "100", "tradeOpened": {"tradeID": "200"}},
        }
        order_id, trade_id = parse_order_ids(resp)
        self.assertEqual(order_id, "100")
        self.assertEqual(trade_id, "200")

    def test_empty_response(self):
        order_id, trade_id = parse_order_ids({})
        self.assertIsNone(order_id)
        self.assertIsNone(trade_id)


class TestParseRealizedPl(unittest.TestCase):
    def test_reads_pl_field(self):
        resp = {"orderFillTransaction": {"pl": "12.34"}}
        self.assertAlmostEqual(parse_realized_pl(resp), 12.34)

    def test_sums_trades_closed(self):
        resp = {"orderFillTransaction": {"tradesClosed": [
            {"realizedPL": "5.0"}, {"realizedPL": "3.0"}
        ]}}
        self.assertAlmostEqual(parse_realized_pl(resp), 8.0)

    def test_returns_zero_on_empty(self):
        self.assertEqual(parse_realized_pl({}), 0.0)


class TestPipBuffer(unittest.TestCase):
    def test_xauusd(self):
        self.assertAlmostEqual(pip_buffer_for_instrument("XAU_USD"), 0.1)

    def test_jpy_pair(self):
        self.assertAlmostEqual(pip_buffer_for_instrument("USD_JPY"), 0.01)
        self.assertAlmostEqual(pip_buffer_for_instrument("GBP_JPY"), 0.01)

    def test_standard_pair(self):
        self.assertAlmostEqual(pip_buffer_for_instrument("EUR_USD"), 0.0001)


class TestGranularitySeconds(unittest.TestCase):
    def test_known_granularities(self):
        cases = [("M1", 60), ("M15", 900), ("H1", 3600), ("D", 86400)]
        for g, expected in cases:
            with self.subTest(g=g):
                self.assertEqual(granularity_to_seconds(g), expected)

    def test_unknown_raises(self):
        with self.assertRaises(ValueError):
            granularity_to_seconds("W1")


class TestSecondsUntilNextClose(unittest.TestCase):
    def test_returns_positive_value(self):
        now = datetime(2024, 1, 15, 8, 7, 30, tzinfo=timezone.utc)
        secs = seconds_until_next_close("M15", now=now)
        self.assertGreater(secs, 0)
        self.assertLessEqual(secs, 900)

    def test_boundary_alignment(self):
        # 08:00:00 exactly → next M15 boundary is 08:15:00 → 900s
        now = datetime(2024, 1, 15, 8, 0, 0, tzinfo=timezone.utc)
        secs = seconds_until_next_close("M15", now=now)
        self.assertEqual(secs, 900)

    def test_daily_boundary(self):
        now = datetime(2024, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
        secs = seconds_until_next_close("D", now=now)
        self.assertEqual(secs, 86400)


if __name__ == "__main__":
    unittest.main()
