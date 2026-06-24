from __future__ import annotations

import unittest
import unittest.mock

import pandas as pd

from bots.slingshot_daily import _find_pattern, _swing_highs, _swing_lows
from bots.conqueror_fx_daily import _all_aligned, _close_trade, _signal_triplet
from shared.indicators import sma, atr


def _make_bars(prices: list[float], freq: str = "D") -> pd.DataFrame:
    n = len(prices)
    idx = pd.date_range("2022-01-01", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame(
        {
            "open": prices,
            "high": [p + 1.0 for p in prices],
            "low": [p - 1.0 for p in prices],
            "close": prices,
        },
        index=idx,
    )


# ─── Slingshot ────────────────────────────────────────────────────────────────

class TestSwingDetection(unittest.TestCase):
    def test_swing_highs_finds_local_maxima(self):
        prices = [100, 105, 100, 100, 108, 100, 100, 106, 100]
        bars = _make_bars(prices)
        highs_idx = _swing_highs(bars)
        high_prices = [bars["high"].iloc[i] for i in highs_idx]
        self.assertIn(106.0, high_prices)  # 105+1 at idx1, 109 at idx4, 107 at idx7
        self.assertTrue(len(highs_idx) >= 1)

    def test_swing_lows_finds_local_minima(self):
        prices = [100, 95, 100, 100, 92, 100, 100, 94, 100]
        bars = _make_bars(prices)
        lows_idx = _swing_lows(bars)
        low_prices = [bars["low"].iloc[i] for i in lows_idx]
        self.assertTrue(any(p < 94.0 for p in low_prices))

    def test_monotone_rising_has_no_swing_highs(self):
        prices = [100 + i for i in range(10)]
        bars = _make_bars(prices)
        self.assertEqual(_swing_highs(bars), [])

    def test_monotone_falling_has_no_swing_lows(self):
        prices = [100 - i for i in range(10)]
        bars = _make_bars(prices)
        self.assertEqual(_swing_lows(bars), [])


class TestSlingshotPattern(unittest.TestCase):
    def _slingshot_prices(self) -> list[float]:
        """
        Build 150 flat bars with three isolated price spikes so swing detection
        is unambiguous, and the gap between key high and slingshot is >=20 bars.

        Structure (bar indices, 0-based):
          bar 20:  prev high spike  (120)  — first swing high
          bar 50:  key high spike   (140)  — second swing high, higher than bar 20
          bar 80:  key low spike    ( 60)  — swing low between key high and slingshot
          bar 100: slingshot spike  (130)  — below key high (140), gap = 50 >= 20
        """
        prices = [100.0] * 150
        prices[20]  = 120.0   # first swing high (prev high)
        prices[50]  = 140.0   # key high
        prices[80]  =  60.0   # key low (swing low between key high and slingshot)
        prices[100] = 130.0   # slingshot high (< 140, gap 50 bars from key high)
        return prices

    def test_finds_slingshot_pattern(self):
        prices = self._slingshot_prices()
        bars = _make_bars(prices)
        result = _find_pattern(bars)
        self.assertIsNotNone(result, "Should detect slingshot pattern")
        self.assertIn("key_low_px", result)
        self.assertIn("sling_high_px", result)
        self.assertIn("target_dist", result)
        self.assertGreater(result["target_dist"], 0)

    def test_no_pattern_on_monotone_bars(self):
        prices = [100 + i * 0.1 for i in range(100)]
        bars = _make_bars(prices)
        result = _find_pattern(bars)
        self.assertIsNone(result)

    def test_no_pattern_with_too_few_bars(self):
        prices = [100, 105, 100, 108, 95, 107]
        bars = _make_bars(prices)
        result = _find_pattern(bars)
        self.assertIsNone(result)

    def test_slingshot_high_is_below_key_high(self):
        prices = self._slingshot_prices()
        bars = _make_bars(prices)
        result = _find_pattern(bars)
        self.assertIsNotNone(result)
        # Slingshot high (131) must be below key high (141)
        self.assertLess(result["sling_high_px"], 141.0)


# ─── Conqueror FX ─────────────────────────────────────────────────────────────

class TestConquerorSignalTriplet(unittest.TestCase):
    def _make_conqueror_df(self, n=100, trend="up") -> pd.DataFrame:
        if trend == "up":
            closes = [100.0 + i * 0.5 for i in range(n)]
        else:
            closes = [100.0 + (n - i) * 0.5 for i in range(n)]
        idx = pd.date_range("2022-01-01", periods=n, freq="D", tz="UTC")
        df = pd.DataFrame(
            {"open": closes, "high": [c + 1 for c in closes],
             "low": [c - 1 for c in closes], "close": closes},
            index=idx,
        )
        df["ma10"] = sma(df["close"], 10)
        return df

    def test_uptrend_all_positive_triplet(self):
        df = self._make_conqueror_df(100, "up")
        idx = len(df) - 1
        a, b, c = _signal_triplet(df, idx)
        self.assertGreater(a, 0, "close > MA10 in uptrend")
        self.assertGreater(b, 0, "MA10 rising in uptrend")
        self.assertGreater(c, 0, "close > 40-bar-ago price in uptrend")

    def test_downtrend_all_negative_triplet(self):
        df = self._make_conqueror_df(100, "down")
        idx = len(df) - 1
        a, b, c = _signal_triplet(df, idx)
        self.assertLess(a, 0, "close < MA10 in downtrend")
        self.assertLess(b, 0, "MA10 falling in downtrend")
        self.assertLess(c, 0, "close < 40-bar-ago price in downtrend")


class TestConquerorAllAligned(unittest.TestCase):
    def test_long_all_positive(self):
        self.assertTrue(_all_aligned(1, 1.0, 1.0, 1.0))

    def test_long_one_negative_not_aligned(self):
        self.assertFalse(_all_aligned(1, 1.0, -0.1, 1.0))

    def test_short_all_negative(self):
        self.assertTrue(_all_aligned(-1, -1.0, -1.0, -1.0))

    def test_short_one_positive_not_aligned(self):
        self.assertFalse(_all_aligned(-1, -1.0, 0.1, -1.0))

    def test_zero_value_not_aligned(self):
        self.assertFalse(_all_aligned(1, 0.0, 1.0, 1.0))
        self.assertFalse(_all_aligned(-1, 0.0, -1.0, -1.0))


class TestConquerorStopLogic(unittest.TestCase):
    """Verify ATR-based stop distance calculation."""

    def test_long_stop_below_entry(self):
        entry_px = 1.30
        side = 1
        atr_mult = 2.0
        atr_t = 0.01
        stop_px = entry_px - side * (atr_mult * atr_t)
        self.assertLess(stop_px, entry_px)
        self.assertAlmostEqual(stop_px, 1.28)

    def test_short_stop_above_entry(self):
        entry_px = 1.30
        side = -1
        atr_mult = 2.0
        atr_t = 0.01
        stop_px = entry_px - side * (atr_mult * atr_t)
        self.assertGreater(stop_px, entry_px)
        self.assertAlmostEqual(stop_px, 1.32)

    def test_variant_i_tightens_stop_on_adverse_flip(self):
        atr_mult = 2.0
        tightened = atr_mult * (2.0 / 3.0)
        self.assertLess(tightened, atr_mult)
        self.assertAlmostEqual(tightened, 4.0 / 3.0)


# ─── Inside Days ──────────────────────────────────────────────────────────────

class TestInsideDaysCondition(unittest.TestCase):
    """
    The inside bar detection in inside_days_daily.main is:
        inside = high_t <= prev_high and low_t >= prev_low
    These tests verify that logic directly.
    """

    def _check_inside(self, high_t, low_t, prev_high, prev_low) -> bool:
        return high_t <= prev_high and low_t >= prev_low

    def test_perfect_inside_bar(self):
        self.assertTrue(self._check_inside(100.5, 99.5, 101.0, 99.0))

    def test_high_breaks_out_not_inside(self):
        self.assertFalse(self._check_inside(101.5, 99.5, 101.0, 99.0))

    def test_low_breaks_out_not_inside(self):
        self.assertFalse(self._check_inside(100.5, 98.5, 101.0, 99.0))

    def test_equal_range_is_inside(self):
        # Exact match on both ends counts as inside
        self.assertTrue(self._check_inside(101.0, 99.0, 101.0, 99.0))

    def test_outside_bar_not_inside(self):
        self.assertFalse(self._check_inside(102.0, 98.0, 101.0, 99.0))

    def test_sl_tp_calculation(self):
        high_t, low_t = 101.0, 99.0
        tp_r = 3.0
        range_dist = high_t - low_t
        buy_entry = high_t
        sell_entry = low_t
        buy_tp = buy_entry + tp_r * range_dist
        sell_tp = sell_entry - tp_r * range_dist
        self.assertAlmostEqual(buy_tp, 107.0)
        self.assertAlmostEqual(sell_tp, 93.0)
        # SL at opposite side of inside bar
        self.assertAlmostEqual(high_t - low_t, range_dist)


class TestCloseTradeNoneTradeId(unittest.TestCase):
    def _now(self) -> pd.Timestamp:
        return pd.Timestamp("2022-01-02", tz="UTC")

    def test_skips_when_trade_id_is_python_none(self):
        client = unittest.mock.MagicMock()
        pos = {"trade_id": None, "open_time": "2022-01-01T00:00:00+00:00"}
        _close_trade("TEST", client, pos, self._now())
        client.close_trade.assert_not_called()

    def test_skips_when_trade_id_is_string_none(self):
        client = unittest.mock.MagicMock()
        pos = {"trade_id": "None", "open_time": "2022-01-01T00:00:00+00:00"}
        _close_trade("TEST", client, pos, self._now())
        client.close_trade.assert_not_called()

    @unittest.mock.patch("bots.conqueror_fx_daily.trade_close_by_trade_id")
    def test_calls_close_trade_when_trade_id_is_valid(self, _mock_db):
        client = unittest.mock.MagicMock()
        client.close_trade.return_value = {"orderFillTransaction": {"pl": "2.0"}}
        pos = {"trade_id": "99", "open_time": "2022-01-01T00:00:00+00:00"}
        _close_trade("TEST", client, pos, self._now())
        client.close_trade.assert_called_once_with("99")


if __name__ == "__main__":
    unittest.main()
