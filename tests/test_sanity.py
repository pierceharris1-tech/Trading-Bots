from __future__ import annotations

import unittest

import pandas as pd

from bots.channel_breakout_daily import _can_take_long_hh55
from shared.bot_config import require_account_id
from shared.indicators import adx, atr, sma
from shared.strategy_utils import candles_to_df


class TestSanity(unittest.TestCase):
    def test_require_account_id(self) -> None:
        self.assertEqual(require_account_id({"account_id": "abc"}), "abc")
        with self.assertRaises(RuntimeError):
            require_account_id({"bot_id": "X"})

    def test_indicators_compute(self) -> None:
        idx = pd.date_range("2024-01-01", periods=120, tz="UTC")
        close = pd.Series([100 + i * 0.5 for i in range(120)], index=idx)
        high = close + 1
        low = close - 1
        df = pd.DataFrame({"open": close, "high": high, "low": low, "close": close}, index=idx)

        self.assertFalse(sma(df["close"], 10).iloc[-1] != sma(df["close"], 10).iloc[-1])
        self.assertFalse(atr(df, 40).iloc[-1] != atr(df, 40).iloc[-1])
        self.assertFalse(adx(df, 14).iloc[-1] != adx(df, 14).iloc[-1])

    def test_channel_long_instant_rule(self) -> None:
        hh = pd.Series([10, 10, 9, 9, 8, 8, 7, 7, 7], dtype=float)
        self.assertTrue(_can_take_long_hh55(hh, 8))

    def test_candles_to_df_rejects_empty(self) -> None:
        with self.assertRaises(RuntimeError):
            candles_to_df({"candles": [{"complete": False}]})


if __name__ == "__main__":
    unittest.main()

