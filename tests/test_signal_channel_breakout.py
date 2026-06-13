from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from bots.channel_breakout_daily import (
    _can_take_long_hh55,
    _process_channel_breakout,
    _process_reversal_days,
)
from shared.indicators import adx, sma


def _make_daily_bars(n: int, prices: list[float] | None = None) -> pd.DataFrame:
    idx = pd.date_range("2020-01-01", periods=n, freq="D", tz="UTC")
    if prices is None:
        prices = [100.0 + i * 0.1 for i in range(n)]
    return pd.DataFrame(
        {
            "open": prices,
            "high": [p + 1.0 for p in prices],
            "low": [p - 1.0 for p in prices],
            "close": prices,
        },
        index=idx,
    )


class TestCanTakeLongHH55(unittest.TestCase):
    def test_monotonically_declining_allows_long(self):
        hh = pd.Series([10, 10, 9, 9, 8, 8, 7, 7, 7], dtype=float)
        self.assertTrue(_can_take_long_hh55(hh, 8))

    def test_rising_hh55_blocks_long(self):
        # HH55 is rising → recent expansion, not in a pullback
        hh = pd.Series([7, 7, 8, 8, 9, 9, 10, 10, 10], dtype=float)
        self.assertFalse(_can_take_long_hh55(hh, 8))

    def test_flat_hh55_allows_long(self):
        hh = pd.Series([10, 10, 10, 10, 10, 10, 10, 10, 10], dtype=float)
        self.assertTrue(_can_take_long_hh55(hh, 8))


class TestChannelBreakoutSignal(unittest.TestCase):
    """
    Tests _process_channel_breakout with mocked OandaClient and KV store.
    We verify order placement is called (or not) based on price vs 55-day channel.
    """

    def _make_bars_with_breakout(self, n=120) -> pd.DataFrame:
        """
        Build bars that genuinely trigger a long channel breakout:
        - Rise to a peak at bar 59, then fall for 60 bars so HH55 is declining
          (the peak rolls out of the 55-bar window around bar 114)
        - Spike the last bar to 300 so close > HH55 and _can_take_long_hh55 passes
        """
        prices = [100.0 + i for i in range(60)]       # bars 0-59: rise 100→159
        prices += [159.0 - i * 0.5 for i in range(60)]  # bars 60-119: fall
        prices[-1] = 300.0                             # last bar: breakout
        return _make_daily_bars(n, prices)

    def _make_bars_no_breakout(self, n=120) -> pd.DataFrame:
        """Flat bars — no channel breakout."""
        prices = [100.0] * n
        return _make_daily_bars(n, prices)

    @patch("bots.channel_breakout_daily.adx")
    @patch("bots.channel_breakout_daily.kv_json_get", return_value=None)
    @patch("bots.channel_breakout_daily.kv_json_set")
    @patch("bots.channel_breakout_daily.trade_open")
    @patch("bots.channel_breakout_daily.get_equity", return_value=10_000.0)
    def test_long_breakout_places_order(self, mock_equity, mock_trade_open, mock_kv_set, mock_kv_get, mock_adx):
        # Patch ADX to return a rising series (last value > second-to-last)
        import numpy as np
        n = 120
        adx_vals = pd.Series(np.linspace(10, 30, n),
                             index=pd.date_range("2020-01-01", periods=n, freq="D", tz="UTC"))
        mock_adx.return_value = adx_vals

        client = MagicMock()
        client.place_market_order.return_value = {
            "orderCreateTransaction": {"id": "1"},
            "orderFillTransaction": {"orderID": "1", "tradeOpened": {"tradeID": "2"}},
        }
        bars = self._make_bars_with_breakout()
        _process_channel_breakout(
            bot_id="TEST", account_id="acct-001", client=client,
            instrument="EUR_USD", bars=bars, cfg={"leverage": 20.0},
        )
        client.place_market_order.assert_called_once()
        call_kwargs = client.place_market_order.call_args
        units = call_kwargs.kwargs.get("units", call_kwargs.args[0] if call_kwargs.args else None)
        self.assertGreater(units, 0, "Long breakout should produce positive units")

    @patch("bots.channel_breakout_daily.kv_json_get", return_value=None)
    @patch("bots.channel_breakout_daily.kv_json_set")
    @patch("bots.channel_breakout_daily.get_equity", return_value=10_000.0)
    def test_no_breakout_no_order(self, mock_equity, mock_kv_set, mock_kv_get):
        client = MagicMock()
        bars = self._make_bars_no_breakout()
        _process_channel_breakout(
            bot_id="TEST", account_id="acct-001", client=client,
            instrument="EUR_USD", bars=bars, cfg={"leverage": 20.0},
        )
        client.place_market_order.assert_not_called()

    @patch("bots.channel_breakout_daily.kv_json_get", return_value=None)
    @patch("bots.channel_breakout_daily.kv_json_set")
    @patch("bots.channel_breakout_daily.get_equity", return_value=10_000.0)
    def test_too_few_bars_skips(self, mock_equity, mock_kv_set, mock_kv_get):
        client = MagicMock()
        bars = _make_daily_bars(30)  # < 80 required
        _process_channel_breakout(
            bot_id="TEST", account_id="acct-001", client=client,
            instrument="EUR_USD", bars=bars, cfg={},
        )
        client.place_market_order.assert_not_called()


class TestReversalDaysSignal(unittest.TestCase):
    def _bars_with_bull_reversal(self) -> pd.DataFrame:
        """
        Build bars where the last bar shows a bull reversal:
        - lower low than previous
        - higher close than previous
        - MA7 rising
        """
        n = 20
        # Steadily rising prices for MA7 to slope up
        prices = [100.0 + i * 0.5 for i in range(n)]
        highs = [p + 1.0 for p in prices]
        lows = [p - 1.0 for p in prices]
        closes = list(prices)

        # Last bar: lower low, higher close
        lows[-1] = lows[-2] - 0.5    # lower low
        closes[-1] = closes[-2] + 0.5  # higher close

        idx = pd.date_range("2022-01-01", periods=n, freq="D", tz="UTC")
        return pd.DataFrame(
            {"open": prices, "high": highs, "low": lows, "close": closes}, index=idx
        )

    def _bars_no_reversal(self) -> pd.DataFrame:
        n = 20
        prices = [100.0] * n
        idx = pd.date_range("2022-01-01", periods=n, freq="D", tz="UTC")
        return pd.DataFrame(
            {"open": prices, "high": [p + 1 for p in prices],
             "low": [p - 1 for p in prices], "close": prices},
            index=idx,
        )

    @patch("bots.channel_breakout_daily.kv_json_get", return_value=None)
    @patch("bots.channel_breakout_daily.kv_json_set")
    @patch("bots.channel_breakout_daily.trade_open")
    @patch("bots.channel_breakout_daily.get_equity", return_value=10_000.0)
    def test_bull_reversal_places_long(self, mock_equity, mock_trade_open, mock_kv_set, mock_kv_get):
        client = MagicMock()
        client.place_market_order.return_value = {
            "orderCreateTransaction": {"id": "1"},
            "orderFillTransaction": {"orderID": "1", "tradeOpened": {"tradeID": "2"}},
        }
        bars = self._bars_with_bull_reversal()
        _process_reversal_days(
            bot_id="TEST", account_id="acct-001", client=client,
            instrument="EUR_USD", bars=bars, cfg={"leverage": 20.0},
        )
        client.place_market_order.assert_called_once()
        units = client.place_market_order.call_args.kwargs["units"]
        self.assertGreater(units, 0)

    @patch("bots.channel_breakout_daily.kv_json_get", return_value=None)
    @patch("bots.channel_breakout_daily.kv_json_set")
    @patch("bots.channel_breakout_daily.get_equity", return_value=10_000.0)
    def test_no_reversal_no_order(self, mock_equity, mock_kv_set, mock_kv_get):
        client = MagicMock()
        bars = self._bars_no_reversal()
        _process_reversal_days(
            bot_id="TEST", account_id="acct-001", client=client,
            instrument="EUR_USD", bars=bars, cfg={},
        )
        client.place_market_order.assert_not_called()


class TestIndicators(unittest.TestCase):
    def _make_df(self, n=120, trend="up") -> pd.DataFrame:
        idx = pd.date_range("2024-01-01", periods=n, tz="UTC")
        if trend == "up":
            close = pd.Series([100 + i * 0.5 for i in range(n)], index=idx)
        else:
            close = pd.Series([100 - i * 0.5 for i in range(n)], index=idx)
        return pd.DataFrame(
            {"open": close, "high": close + 1, "low": close - 1, "close": close}, index=idx
        )

    def test_sma_last_value_is_trailing_average(self):
        df = self._make_df()
        result = sma(df["close"], 10)
        expected = df["close"].iloc[-10:].mean()
        self.assertAlmostEqual(result.iloc[-1], expected, places=5)

    def test_sma_is_nan_before_warmup(self):
        df = self._make_df(20)
        result = sma(df["close"], 10)
        self.assertTrue(pd.isna(result.iloc[8]))   # period=10, warmup ends at index 9
        self.assertFalse(pd.isna(result.iloc[9]))

    def test_adx_returns_positive_in_trend(self):
        df = self._make_df(100)
        result = adx(df, 14)
        self.assertGreater(result.iloc[-1], 0)

    def test_adx_length_matches_input(self):
        df = self._make_df(100)
        result = adx(df, 14)
        self.assertEqual(len(result), len(df))


if __name__ == "__main__":
    unittest.main()
