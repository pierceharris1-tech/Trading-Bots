from __future__ import annotations

import unittest
from datetime import datetime, timezone

from shared.time_utils import granularity_to_seconds, seconds_until_next_close


class TestChochM10Timing(unittest.TestCase):
    def test_m10_supported(self):
        self.assertEqual(granularity_to_seconds("M10"), 600)

    def test_next_close_on_ten_minute_boundary(self):
        now = datetime(2026, 1, 1, 12, 3, 0, tzinfo=timezone.utc)
        self.assertEqual(seconds_until_next_close("M10", now), 420)

    def test_next_close_hour_rollover(self):
        now = datetime(2026, 1, 1, 12, 59, 30, tzinfo=timezone.utc)
        self.assertEqual(seconds_until_next_close("M10", now), 30)


if __name__ == "__main__":
    unittest.main()

