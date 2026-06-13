from __future__ import annotations

import unittest

from shared.risk import clamp_units, fixed_units_for_practice, units_from_risk


class TestUnitsFromRisk(unittest.TestCase):
    def _calc(self, **kw):
        defaults = dict(
            equity=10_000.0,
            risk_pct=0.01,
            entry=1.30,
            stop=1.29,
            leverage=20.0,
        )
        defaults.update(kw)
        return units_from_risk(**defaults)

    def test_basic_calculation(self):
        # equity=10000, risk=1%, stop_dist≈0.01 → risk_cash=100, units≈10000
        # stop_dist = |1.30-1.29| has floating-point representation error, so units
        # may be 9999 rather than 10000 — allow delta=1.
        units, risk_cash, margin = self._calc()
        self.assertAlmostEqual(units, 10_000, delta=1)
        self.assertAlmostEqual(risk_cash, 100.0)
        self.assertAlmostEqual(margin, units * 1.30 / 20.0, places=4)

    def test_zero_equity(self):
        units, risk_cash, margin = self._calc(equity=0.0)
        self.assertEqual(units, 0)
        self.assertEqual(risk_cash, 0.0)

    def test_negative_equity(self):
        units, risk_cash, margin = self._calc(equity=-100.0)
        self.assertEqual(units, 0)

    def test_zero_stop_distance(self):
        units, risk_cash, margin = self._calc(entry=1.30, stop=1.30)
        self.assertEqual(units, 0)

    def test_min_units_enforcement(self):
        # Very small equity so raw units < min_units
        units, _, _ = self._calc(equity=1.0, risk_pct=0.001, entry=1.30, stop=1.299, min_units=100)
        self.assertEqual(units, 0)

    def test_max_units_clamping(self):
        units, _, _ = self._calc(equity=10_000_000.0, max_units=50_000)
        self.assertEqual(units, 50_000)

    def test_quote_to_account_rate_scales_units(self):
        # Doubling the conversion rate should double units
        u1, _, _ = self._calc(quote_to_account_rate=1.0)
        u2, _, _ = self._calc(quote_to_account_rate=2.0)
        self.assertAlmostEqual(u2, u1 * 2, delta=1)

    def test_wider_stop_reduces_units(self):
        u_tight, _, _ = self._calc(entry=1.30, stop=1.29)   # 0.01 stop
        u_wide, _, _ = self._calc(entry=1.30, stop=1.28)    # 0.02 stop
        self.assertAlmostEqual(u_wide, u_tight // 2, delta=1)

    def test_higher_risk_pct_increases_units(self):
        u1, _, _ = self._calc(risk_pct=0.01)
        u2, _, _ = self._calc(risk_pct=0.02)
        # Integer truncation of floating-point arithmetic may cause off-by-one
        self.assertAlmostEqual(u2, u1 * 2, delta=1)

    def test_jpy_pair_sizing(self):
        # USD/JPY: entry=150, stop=149, stop_dist=1 pip-ish
        # quote_to_account_rate = GBP_JPY rate (e.g. 190)
        units, risk_cash, _ = units_from_risk(
            equity=10_000.0,
            risk_pct=0.01,
            entry=150.0,
            stop=149.0,
            leverage=30.0,
            quote_to_account_rate=190.0,
        )
        expected_units = int((10_000.0 * 0.01 * 190.0) / 1.0)
        self.assertEqual(units, expected_units)

    def test_gold_sizing(self):
        # XAU/USD: entry=2000, stop=1990, stop_dist=10
        units, risk_cash, _ = units_from_risk(
            equity=10_000.0,
            risk_pct=0.01,
            entry=2000.0,
            stop=1990.0,
            leverage=20.0,
            quote_to_account_rate=1.27,
        )
        expected_units = int((10_000.0 * 0.01 * 1.27) / 10.0)
        self.assertEqual(units, expected_units)


class TestClampUnits(unittest.TestCase):
    def test_zero_passthrough(self):
        self.assertEqual(clamp_units(0), 0)

    def test_positive_within_limit(self):
        self.assertEqual(clamp_units(1000), 1000)

    def test_positive_exceeds_limit(self):
        self.assertEqual(clamp_units(100_000), 50_000)

    def test_negative_within_limit(self):
        self.assertEqual(clamp_units(-1000), -1000)

    def test_negative_exceeds_limit(self):
        self.assertEqual(clamp_units(-100_000), -50_000)

    def test_custom_max(self):
        self.assertEqual(clamp_units(200, max_abs=100), 100)
        self.assertEqual(clamp_units(-200, max_abs=100), -100)


class TestFixedUnitsPractice(unittest.TestCase):
    def test_returns_positive_int(self):
        result = fixed_units_for_practice()
        self.assertIsInstance(result, int)
        self.assertGreater(result, 0)


if __name__ == "__main__":
    unittest.main()
