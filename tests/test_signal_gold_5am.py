from __future__ import annotations

import unittest

import pandas as pd

from bots.gold_5am_breakout import (
    find_break_candle,
    find_best_trendline,
    find_swing_points,
    get_atr,
    get_daily_trend,
    get_line_price,
    get_trend_direction,
    validate_trendline,
)


def _make_m5_day(n=72, base=2000.0) -> pd.DataFrame:
    """72 × 5-min bars = 6 hours starting midnight UTC, tz-naive to match bot code."""
    idx = pd.date_range("2024-01-15 00:00", periods=n, freq="5min")
    prices = [base + i * 0.1 for i in range(n)]
    return pd.DataFrame(
        {"open": prices, "high": [p + 0.5 for p in prices],
         "low": [p - 0.5 for p in prices], "close": prices},
        index=idx,
    )


def _make_daily_bars(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {"open": closes, "high": [c + 1 for c in closes],
         "low": [c - 1 for c in closes], "close": closes},
        index=idx,
    )


class TestGetTrendDirection(unittest.TestCase):
    def test_uptrend_detected(self):
        df = _make_m5_day(72, base=2000.0)
        # midnight close ~2000, 3:55am close is bar 47 (47×5min = 235min = 3h55m)
        # Already trending up by construction
        result = get_trend_direction(df)
        self.assertEqual(result, "up")

    def test_downtrend_detected(self):
        df = _make_m5_day(72, base=2000.0)
        # Reverse prices so they go down over time
        prices = [2000.0 - i * 0.1 for i in range(72)]
        df["close"] = prices
        result = get_trend_direction(df)
        self.assertEqual(result, "down")

    def test_flat_returns_none(self):
        df = _make_m5_day(72)
        df["close"] = 2000.0  # perfectly flat
        result = get_trend_direction(df)
        self.assertIsNone(result)

    def test_missing_midnight_bar_returns_none(self):
        # Only afternoon bars — no midnight candle
        idx = pd.date_range("2024-01-15 12:00", periods=12, freq="5min")
        df = pd.DataFrame(
            {"open": [2000.0] * 12, "high": [2001.0] * 12,
             "low": [1999.0] * 12, "close": [2000.0] * 12},
            index=idx,
        )
        result = get_trend_direction(df)
        self.assertIsNone(result)


class TestGetDailyTrend(unittest.TestCase):
    def test_two_up_days_returns_up(self):
        bars = _make_daily_bars([100.0, 101.0, 102.0, 103.0])
        result = get_daily_trend(bars)
        self.assertEqual(result, "up")

    def test_two_down_days_returns_down(self):
        bars = _make_daily_bars([103.0, 102.0, 101.0, 100.0])
        result = get_daily_trend(bars)
        self.assertEqual(result, "down")

    def test_mixed_days_returns_none(self):
        bars = _make_daily_bars([100.0, 102.0, 101.0, 103.0])
        result = get_daily_trend(bars)
        self.assertIsNone(result)

    def test_too_few_bars_returns_none(self):
        bars = _make_daily_bars([100.0, 101.0])
        result = get_daily_trend(bars)
        self.assertIsNone(result)


class TestGetAtr(unittest.TestCase):
    def test_returns_positive_float(self):
        df = _make_m5_day(30)
        result = get_atr(df, period=14)
        self.assertGreater(result, 0.0)

    def test_wider_bars_produce_larger_atr(self):
        n = 30
        idx = pd.date_range("2024-01-15", periods=n, freq="5min")
        narrow = pd.DataFrame(
            {"high": [100.1] * n, "low": [99.9] * n, "close": [100.0] * n}, index=idx
        )
        wide = pd.DataFrame(
            {"high": [102.0] * n, "low": [98.0] * n, "close": [100.0] * n}, index=idx
        )
        self.assertGreater(get_atr(wide, 14), get_atr(narrow, 14))

    def test_single_bar_returns_float(self):
        df = _make_m5_day(1)
        result = get_atr(df, 14)
        self.assertIsInstance(result, float)


class TestFindSwingPoints(unittest.TestCase):
    def _bars_with_clear_peaks(self) -> pd.DataFrame:
        highs = [100, 102, 100, 100, 103, 100, 100, 101, 100]
        lows  = [98,  98,  98,  97,  98,  98,  96,  98,  98]
        idx = pd.date_range("2024-01-01", periods=len(highs), freq="5min")
        return pd.DataFrame(
            {"high": highs, "low": lows,
             "close": [100.0] * len(highs), "open": [100.0] * len(highs)},
            index=idx,
        )

    def test_finds_resistance_swing_highs(self):
        bars = self._bars_with_clear_peaks()
        points = find_swing_points(bars, n=1, direction="resistance")
        prices = [p for _, p in points]
        self.assertIn(102.0, prices)
        self.assertIn(103.0, prices)

    def test_finds_support_swing_lows(self):
        bars = self._bars_with_clear_peaks()
        points = find_swing_points(bars, n=1, direction="support")
        prices = [p for _, p in points]
        self.assertIn(97.0, prices)
        self.assertIn(96.0, prices)

    def test_monotone_declining_has_no_resistance_swings(self):
        # Strictly falling highs — no bar is above both neighbours
        highs = [100.0 - i for i in range(10)]
        lows  = [h - 1 for h in highs]
        idx = pd.date_range("2024-01-01", periods=10, freq="5min")
        bars = pd.DataFrame(
            {"high": highs, "low": lows,
             "close": lows, "open": lows},
            index=idx,
        )
        points = find_swing_points(bars, n=1, direction="resistance")
        self.assertEqual(len(points), 0)


class TestGetLinePriceAndValidate(unittest.TestCase):
    def test_line_price_interpolates(self):
        p1 = (0, 10.0)
        p2 = (10, 20.0)
        self.assertAlmostEqual(get_line_price(p1, p2, 5), 15.0)

    def test_vertical_line_returns_y1(self):
        p1 = (5, 10.0)
        p2 = (5, 20.0)
        self.assertAlmostEqual(get_line_price(p1, p2, 5), 10.0)

    def test_resistance_trendline_broken_by_close_invalidates(self):
        """A candle that closes above a resistance trendline should invalidate it."""
        n = 10
        idx = pd.date_range("2024-01-01", periods=n, freq="5min")
        # Declining resistance line from 110 to 100
        highs = [110 - i for i in range(n)]
        closes = [100.0] * n
        closes[5] = 110.0  # This close breaks above the resistance line
        df = pd.DataFrame(
            {"high": highs, "low": [99.0] * n, "close": closes, "open": closes},
            index=idx,
        )
        p1 = (0, 110.0)
        p2 = (9, 101.0)
        valid, count, _ = validate_trendline(df, p1, p2, "resistance", atr=1.0)
        self.assertFalse(valid)


class TestFindBreakCandle(unittest.TestCase):
    def _make_entry_window(self, n=12, base=2000.0) -> pd.DataFrame:
        idx = pd.date_range("2024-01-15 04:00", periods=n, freq="5min")
        prices = [base] * n
        return pd.DataFrame(
            {"high": [p + 0.5 for p in prices], "low": [p - 0.5 for p in prices],
             "close": prices, "open": prices},
            index=idx,
        )

    def test_detects_resistance_break(self):
        df = self._make_entry_window()  # default close=2000 for all bars
        # Set bar 3 close above the line at that x position.
        # Line: p1=(0,2200), p2=(50,2000), slope=-4. At lookback_len=48, k=0: line=2200-4*48=2008 > 2000 (no break).
        # At k=2: line=2200-4*50=2000, close=2000 NOT > 2000. At k=3: line=2200-4*51=1996 < close=2010 → break.
        df.iloc[3, df.columns.get_loc("close")] = 2010.0
        p1 = (0, 2200.0)
        p2 = (50, 2000.0)
        result = find_break_candle(df, lookback_len=48, p1=p1, p2=p2, tl_direction="resistance")
        self.assertIsNotNone(result)
        ts, _ = result
        self.assertEqual(ts, df.index[3])

    def test_no_break_returns_none(self):
        df = self._make_entry_window()  # default close=2000
        # Line stays well above 2000 throughout the entire entry window
        # p1=(0,2500), p2=(100,2400), slope=-1. At k=0 (x=48): 2500-48=2452 > 2000; at k=11 (x=59): 2441 > 2000.
        p1 = (0, 2500.0)
        p2 = (100, 2400.0)
        result = find_break_candle(df, lookback_len=48, p1=p1, p2=p2, tl_direction="resistance")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
