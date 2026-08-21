from __future__ import annotations

import unittest

from shared.daily_engine import select_latest_complete_candle


class TestCandleCompleteSelection(unittest.TestCase):
    def test_selects_last_complete_when_tail_is_incomplete(self) -> None:
        candles = [
            {"time": "2026-01-01T00:00:00Z", "complete": True},
            {"time": "2026-01-01T00:15:00Z", "complete": True},
            {"time": "2026-01-01T00:30:00Z", "complete": False},
        ]
        picked = select_latest_complete_candle(candles)
        self.assertIsNotNone(picked)
        if picked is None:
            self.fail("Expected a complete candle")
        ts, _ = picked
        self.assertEqual(ts.isoformat(), "2026-01-01T00:15:00+00:00")

    def test_selects_last_complete_when_tail_is_complete(self) -> None:
        candles = [
            {"time": "2026-01-01T00:00:00Z", "complete": True},
            {"time": "2026-01-01T00:15:00Z", "complete": True},
        ]
        picked = select_latest_complete_candle(candles)
        self.assertIsNotNone(picked)
        if picked is None:
            self.fail("Expected a complete candle")
        ts, _ = picked
        self.assertEqual(ts.isoformat(), "2026-01-01T00:15:00+00:00")


if __name__ == "__main__":
    unittest.main()
