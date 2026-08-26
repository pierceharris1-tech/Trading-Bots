from __future__ import annotations

import unittest

import pandas as pd

from bots.choch_live.s1_structure import (
    FROZEN_TP_R,
    add_atr,
    block_s1_entry,
    pending_limit_filled,
    qualifying_choch_events,
    retracement_price,
    simulate_s1_event,
)


def _h1_df() -> pd.DataFrame:
    index = pd.date_range("2026-01-05 00:00", periods=45, freq="h", tz="UTC")
    df = pd.DataFrame(
        {
            "open": 102.0,
            "high": 102.5,
            "low": 101.5,
            "close": 102.0,
            "bidopen": 101.99,
            "bidhigh": 102.49,
            "bidlow": 101.49,
            "bidclose": 101.99,
            "askopen": 102.01,
            "askhigh": 102.51,
            "asklow": 101.51,
            "askclose": 102.01,
        },
        index=index,
    )
    entry = retracement_price(100.0, 104.0, "bullish")
    target = entry + FROZEN_TP_R * (entry - 100.0)
    df.iloc[21, df.columns.get_loc("asklow")] = entry - 0.01
    df.iloc[21, df.columns.get_loc("bidlow")] = entry - 0.03
    df.iloc[21, df.columns.get_loc("low")] = entry - 0.02
    df.iloc[21, df.columns.get_loc("bidhigh")] = target + 0.01
    df.iloc[21, df.columns.get_loc("askhigh")] = target + 0.03
    df.iloc[21, df.columns.get_loc("high")] = target + 0.02
    return df


def _event() -> pd.Series:
    return pd.Series(
        {
            "instrument": "EUR_USD",
            "event_time": pd.Timestamp("2026-01-05 20:00", tz="UTC"),
            "bar_pos": 20,
            "type": "CHoCH",
            "direction": "bullish",
            "origin_price": 100.0,
            "event_extreme": 104.0,
            "atr": 2.0,
        }
    )


class TestChochS1Structure(unittest.TestCase):
    def test_atr_is_14_period_simple_true_range_mean(self):
        index = pd.date_range("2026-01-01", periods=15, freq="h", tz="UTC")
        df = pd.DataFrame(
            {
                "open": [10.0] * 15,
                "high": [11.0] * 15,
                "low": [9.0] * 15,
                "close": [10.0] * 15,
            },
            index=index,
        )
        result = add_atr(df)
        self.assertTrue(pd.isna(result.iloc[12]["atr"]))
        self.assertAlmostEqual(result.iloc[13]["atr"], 2.0)
        self.assertAlmostEqual(result.iloc[14]["atr"], 2.0)

    def test_displacement_is_not_initial_choch_filter(self):
        events = pd.DataFrame(
            [
                {
                    "type": "CHoCH",
                    "direction": "bullish",
                    "broken_structure_atr": 1.0,
                    "break_strength_atr": 0.25,
                    "initial_displacement_atr": 1.0,
                }
            ]
        )
        self.assertEqual(len(qualifying_choch_events(events)), 1)

    def test_retracement_and_bid_ask_limit_fill(self):
        entry = retracement_price(100.0, 104.0, "bullish")
        self.assertAlmostEqual(entry, 101.528)
        bar = pd.Series({"asklow": entry - 0.001, "bidlow": entry + 1.0})
        self.assertTrue(pending_limit_filled(bar, "bullish", entry))

    def test_friday_entry_filter_blocks_only_0700_and_0800_utc(self):
        self.assertTrue(block_s1_entry(pd.Timestamp("2026-01-09 07:59", tz="UTC")))
        self.assertTrue(block_s1_entry(pd.Timestamp("2026-01-09 08:00", tz="UTC")))
        self.assertFalse(block_s1_entry(pd.Timestamp("2026-01-09 09:00", tz="UTC")))
        self.assertFalse(block_s1_entry(pd.Timestamp("2026-01-09 13:00", tz="UTC")))
        self.assertFalse(block_s1_entry(pd.Timestamp("2026-01-08 08:00", tz="UTC")))

    def test_s1_fills_and_exits_at_fixed_1p5r_target(self):
        result = simulate_s1_event(_h1_df(), _event(), {})
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status, "filled")
        self.assertEqual(result.outcome, "tp")
        self.assertAlmostEqual(result.entry, 101.528)
        self.assertAlmostEqual(result.stop, 100.0)
        self.assertAlmostEqual(result.target, 103.82)
        self.assertAlmostEqual(result.net_r, 1.5)

    def test_origin_invalidation_cancels_pending_setup(self):
        df = _h1_df()
        df.iloc[21, df.columns.get_loc("asklow")] = 101.8
        df.iloc[21, df.columns.get_loc("bidlow")] = 98.0
        df.iloc[21, df.columns.get_loc("low")] = 99.9
        result = simulate_s1_event(df, _event(), {})
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status, "cancel_origin_break")
        self.assertIsNone(result.entry)

    def test_expiry_boundary_is_strictly_greater_than_20_bars(self):
        df = _h1_df()
        entry = retracement_price(100.0, 104.0, "bullish")
        for pos in range(21, len(df)):
            df.iloc[pos, df.columns.get_loc("asklow")] = entry + 1.0
            df.iloc[pos, df.columns.get_loc("bidlow")] = entry + 0.8
            df.iloc[pos, df.columns.get_loc("low")] = entry + 0.9
            df.iloc[pos, df.columns.get_loc("bidhigh")] = entry + 1.1
            df.iloc[pos, df.columns.get_loc("askhigh")] = entry + 1.2
            df.iloc[pos, df.columns.get_loc("high")] = entry + 1.15
            df.iloc[pos, df.columns.get_loc("close")] = entry + 0.5
        df.iloc[40, df.columns.get_loc("asklow")] = entry - 0.01
        result = simulate_s1_event(df, _event(), {})
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status, "filled")
        self.assertEqual(result.entry_time, df.index[40])

        df.iloc[40, df.columns.get_loc("asklow")] = entry + 1.0
        df.iloc[41, df.columns.get_loc("asklow")] = entry - 0.01
        result = simulate_s1_event(df, _event(), {})
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status, "cancel_expired")
        self.assertIsNone(result.entry)

    def test_dynamic_anchor_updates_until_fill_then_freezes(self):
        df = _h1_df()
        initial_entry = retracement_price(100.0, 104.0, "bullish")
        updated_entry = retracement_price(100.0, 106.0, "bullish")
        df.iloc[21, df.columns.get_loc("asklow")] = initial_entry + 1.0
        df.iloc[21, df.columns.get_loc("bidlow")] = initial_entry + 0.8
        df.iloc[21, df.columns.get_loc("low")] = initial_entry + 0.9
        df.iloc[21, df.columns.get_loc("high")] = 106.0
        df.iloc[21, df.columns.get_loc("bidhigh")] = 105.99
        df.iloc[21, df.columns.get_loc("askhigh")] = 106.01
        df.iloc[21, df.columns.get_loc("close")] = updated_entry + 0.5
        df.iloc[22, df.columns.get_loc("asklow")] = updated_entry - 0.01
        result = simulate_s1_event(df, _event(), {})
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status, "filled")
        self.assertAlmostEqual(result.entry, updated_entry)
        self.assertAlmostEqual(result.event_atr, 2.0)
        self.assertAlmostEqual(result.payload["frozen_extreme"], 106.0)


if __name__ == "__main__":
    unittest.main()
