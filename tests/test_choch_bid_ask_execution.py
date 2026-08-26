from __future__ import annotations

import unittest

from shared.oanda_client import OandaClient
from shared.strategy_utils import bid_ask_candles_to_df, candles_to_df


class TestChochBidAskCandles(unittest.TestCase):
    def test_get_candles_accepts_price_component(self):
        calls = []
        client = OandaClient("https://api-fxpractice.oanda.com", "k", "acct", "BOT", "M10")
        client._request = lambda method, path, **kwargs: calls.append(kwargs) or {"candles": []}
        client.get_candles("EUR_USD", "M10", count=2, price="BA")
        self.assertEqual(calls[0]["params"]["price"], "BA")

    def test_invalid_price_component_rejected(self):
        client = OandaClient("https://api-fxpractice.oanda.com", "k", "acct")
        with self.assertRaises(ValueError):
            client.get_candles("EUR_USD", "M10", price="XYZ")

    def test_midpoint_legacy_behaviour_unchanged(self):
        df = candles_to_df(
            {
                "candles": [
                    {"time": "2026-01-01T00:00:00Z", "complete": True, "mid": {"o": "1", "h": "2", "l": "0.5", "c": "1.5"}},
                    {"time": "2026-01-01T00:10:00Z", "complete": False, "mid": {"o": "9", "h": "9", "l": "9", "c": "9"}},
                ]
            }
        )
        self.assertEqual(len(df), 1)
        self.assertEqual(float(df.iloc[0]["close"]), 1.5)

    def test_bid_response_parses_complete_only(self):
        df = bid_ask_candles_to_df(
            {
                "candles": [
                    {"time": "2026-01-01T00:00:00Z", "complete": True, "bid": {"o": "1", "h": "2", "l": "0.5", "c": "1.5"}},
                    {"time": "2026-01-01T00:10:00Z", "complete": False, "bid": {"o": "9", "h": "9", "l": "9", "c": "9"}},
                ]
            }
        )
        self.assertEqual(len(df), 1)
        self.assertIn("bid_close", df.columns)
        self.assertNotIn("ask_close", df.columns)

    def test_ask_response_parses(self):
        df = bid_ask_candles_to_df(
            {"candles": [{"time": "2026-01-01T00:00:00Z", "complete": True, "ask": {"o": "1.1", "h": "2.1", "l": "0.6", "c": "1.6"}}]}
        )
        self.assertEqual(float(df.iloc[0]["ask_close"]), 1.6)

    def test_bid_ask_response_parses(self):
        df = bid_ask_candles_to_df(
            {
                "candles": [
                    {
                        "time": "2026-01-01T00:00:00Z",
                        "complete": True,
                        "bid": {"o": "1", "h": "2", "l": "0.5", "c": "1.5"},
                        "ask": {"o": "1.1", "h": "2.1", "l": "0.6", "c": "1.6"},
                    }
                ]
            }
        )
        self.assertEqual(float(df.iloc[0]["bid_close"]), 1.5)
        self.assertEqual(float(df.iloc[0]["ask_close"]), 1.6)


if __name__ == "__main__":
    unittest.main()

