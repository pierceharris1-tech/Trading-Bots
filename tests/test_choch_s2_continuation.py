from __future__ import annotations

import unittest
from types import SimpleNamespace

import pandas as pd

from bots.choch_live.s2_continuation import (
    S2_DECISION_BAR,
    build_s2_features,
    s2_direction_for_s1,
    simulate_s2_from_s1,
)


def _m10_df(*, target_hit: bool = True) -> pd.DataFrame:
    index = pd.date_range("2026-01-05 10:00", periods=14, freq="10min", tz="UTC")
    df = pd.DataFrame(
        {
            "open": 116.0,
            "high": 117.0,
            "low": 104.0,
            "close": 110.0,
            "bidopen": 116.0,
            "bidhigh": 117.0,
            "bidlow": 104.0,
            "bidclose": 110.0,
            "askopen": 116.1,
            "askhigh": 117.1,
            "asklow": 104.1,
            "askclose": 110.1,
        },
        index=index,
    )
    closes = [114.0, 113.0, 112.0, 111.0, 110.0, 109.0, 108.0, 107.0, 106.0, 105.0]
    for n, close in enumerate(closes, start=1):
        df.iloc[n, df.columns.get_loc("bidopen")] = close + 1.0
        df.iloc[n, df.columns.get_loc("bidclose")] = close
        df.iloc[n, df.columns.get_loc("bidhigh")] = close + 3.0
        df.iloc[n, df.columns.get_loc("bidlow")] = close - 1.0
        df.iloc[n, df.columns.get_loc("askopen")] = close + 1.1
        df.iloc[n, df.columns.get_loc("askclose")] = close + 0.1
        df.iloc[n, df.columns.get_loc("askhigh")] = close + 3.1
        df.iloc[n, df.columns.get_loc("asklow")] = close - 0.9
        df.iloc[n, df.columns.get_loc("open")] = close + 1.05
        df.iloc[n, df.columns.get_loc("close")] = close + 0.05
        df.iloc[n, df.columns.get_loc("high")] = close + 3.05
        df.iloc[n, df.columns.get_loc("low")] = close - 0.95

    if target_hit:
        df.iloc[11, df.columns.get_loc("askhigh")] = 108.5
        df.iloc[11, df.columns.get_loc("bidhigh")] = 108.4
        df.iloc[11, df.columns.get_loc("high")] = 108.45
        df.iloc[11, df.columns.get_loc("asklow")] = 88.9
        df.iloc[11, df.columns.get_loc("bidlow")] = 88.8
        df.iloc[11, df.columns.get_loc("low")] = 88.85
    return df


def _s1() -> SimpleNamespace:
    return SimpleNamespace(
        outcome="tp",
        exit_time=pd.Timestamp("2026-01-05 10:00", tz="UTC"),
        entry=100.0,
        risk=10.0,
        direction="bullish",
    )


class TestChochS2Continuation(unittest.TestCase):
    def test_post_tp_features_exclude_tp_bar_and_use_original_s1_r(self):
        features = build_s2_features(
            _m10_df(),
            s1_tp_time=pd.Timestamp("2026-01-05 10:00", tz="UTC"),
            original_entry=100.0,
            original_risk=10.0,
            original_direction="bullish",
        )
        self.assertEqual(features.iloc[0]["signal_time"], pd.Timestamp("2026-01-05 10:10", tz="UTC"))
        self.assertAlmostEqual(features.iloc[0]["close_r"], 1.4)
        self.assertAlmostEqual(features.iloc[9]["close_r"], 0.5)
        self.assertEqual(features.iloc[9]["max_against_run"], 10)
        self.assertTrue(features.iloc[9]["lost_1p5"])
        self.assertFalse(features.iloc[9]["reclaimed_after_loss"])
        self.assertEqual(int(features.iloc[9]["bar_num"]), S2_DECISION_BAR)

    def test_s2_reversal_qualifies_after_10_bars_and_hits_4r_target(self):
        result = simulate_s2_from_s1(_m10_df(), parent_s1_key="s1-key", s1=_s1())
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.s2_direction, "bearish")
        self.assertEqual(result.decision_time, pd.Timestamp("2026-01-05 11:40", tz="UTC"))
        self.assertAlmostEqual(result.entry, 105.0)
        self.assertAlmostEqual(result.stop, 109.0)
        self.assertAlmostEqual(result.target, 89.0)
        self.assertEqual(result.outcome, "target")
        self.assertAlmostEqual(result.r, 4.0)

    def test_s2_decision_candle_is_not_reused_for_exit_checks(self):
        df = _m10_df(target_hit=False).iloc[:11].copy()
        df.iloc[10, df.columns.get_loc("askhigh")] = 120.0
        df.iloc[10, df.columns.get_loc("asklow")] = 80.0
        result = simulate_s2_from_s1(df, parent_s1_key="s1-key", s1=_s1())
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.status, "qualified")
        self.assertEqual(result.decision_time, pd.Timestamp("2026-01-05 11:40", tz="UTC"))
        self.assertIsNone(result.exit_time)
        self.assertIsNone(result.outcome)

    def test_s2_outcome_starts_from_bar_after_decision(self):
        result = simulate_s2_from_s1(_m10_df(), parent_s1_key="s1-key", s1=_s1())
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.decision_time, pd.Timestamp("2026-01-05 11:40", tz="UTC"))
        self.assertEqual(result.exit_time, pd.Timestamp("2026-01-05 11:50", tz="UTC"))

    def test_s2_direction_mapping(self):
        self.assertEqual(s2_direction_for_s1("bullish"), "bearish")
        self.assertEqual(s2_direction_for_s1("bearish"), "bullish")

    def test_s2_feature_reclaim_and_loss_boundaries(self):
        index = pd.date_range("2026-01-05 10:00", periods=5, freq="10min", tz="UTC")
        df = pd.DataFrame(
            {
                "open": [115.0, 116.0, 114.0, 113.0, 116.0],
                "high": [116.0, 117.0, 115.0, 114.0, 117.0],
                "low": [114.0, 115.0, 113.0, 112.0, 115.0],
                "close": [115.0, 115.0, 113.0, 112.0, 115.0],
            },
            index=index,
        )
        features = build_s2_features(
            df,
            s1_tp_time=index[0],
            original_entry=100.0,
            original_risk=10.0,
            original_direction="bullish",
        )
        self.assertAlmostEqual(features.iloc[0]["open_r"], 1.6)
        self.assertAlmostEqual(features.iloc[0]["close_r"], 1.5)
        self.assertEqual(features.iloc[0]["against_ratio"], 1.0)
        self.assertFalse(features.iloc[0]["lost_1p5"])
        self.assertTrue(features.iloc[1]["lost_1p5"])
        self.assertFalse(features.iloc[1]["reclaimed_after_loss"])
        self.assertTrue(features.iloc[3]["reclaimed_after_loss"])

    def test_s2_rejects_if_no_s1_tp(self):
        s1 = _s1()
        s1.outcome = "sl"
        self.assertIsNone(simulate_s2_from_s1(_m10_df(), parent_s1_key="s1-key", s1=s1))


if __name__ == "__main__":
    unittest.main()
