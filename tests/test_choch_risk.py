from __future__ import annotations

import unittest

from bots.choch_live.models import InstrumentMetadata
from bots.choch_live.risk import (
    assess_margin_feasibility,
    constrain_shadow_position_to_margin,
    quote_currency,
    size_shadow_position,
)


class TestChochRisk(unittest.TestCase):
    def test_shadow_position_uses_margin_rate_metadata_not_hardcoded_leverage(self):
        metadata = InstrumentMetadata(
            key="eur_usd",
            name="EUR_USD",
            margin_rate=0.03333333333333,
            minimum_trade_size=1.0,
            trade_units_precision=0,
            pip_location=-4,
            display_precision=5,
            instrument_type="CURRENCY",
        )
        size = size_shadow_position(
            account_nav=10_000.0,
            risk_pct=0.02,
            entry=1.1000,
            stop=1.0900,
            direction="bullish",
            metadata=metadata,
            quote_to_account_rate=0.80,
            max_units=100_000,
        )
        self.assertEqual(quote_currency("EUR_USD"), "USD")
        self.assertAlmostEqual(size.risk_cash, 200.0)
        self.assertAlmostEqual(size.risk_per_unit, 0.008)
        self.assertEqual(size.units, 25_000)
        self.assertEqual(size.signed_units, 25_000)
        self.assertAlmostEqual(size.margin_required, 733.33333333326)
        self.assertAlmostEqual(metadata.effective_leverage, 30.0, places=6)

    def test_nas100_shadow_position_respects_decimal_unit_precision_after_conversion(self):
        metadata = InstrumentMetadata(
            key="nasdaq_100",
            name="NAS100_USD",
            margin_rate=0.05,
            minimum_trade_size=0.1,
            trade_units_precision=1,
            pip_location=-1,
            display_precision=1,
            instrument_type="CFD",
        )
        size = size_shadow_position(
            account_nav=10_000.0,
            risk_pct=0.02,
            entry=20_000.0,
            stop=19_900.0,
            direction="bearish",
            metadata=metadata,
            quote_to_account_rate=0.80,
            max_units=1000,
        )
        self.assertAlmostEqual(size.risk_cash, 200.0)
        self.assertAlmostEqual(size.risk_per_unit, 80.0)
        self.assertEqual(size.units, 2.5)
        self.assertEqual(size.signed_units, -2.5)
        self.assertAlmostEqual(size.margin_required, 2_000.0)
        self.assertAlmostEqual(metadata.effective_leverage, 20.0)

    def test_shadow_position_risk_cash_tracks_current_nav_and_units_change(self):
        metadata = InstrumentMetadata(
            key="eur_usd",
            name="EUR_USD",
            margin_rate=0.03333333333333,
            minimum_trade_size=1.0,
            trade_units_precision=0,
            pip_location=-4,
            display_precision=5,
            instrument_type="CURRENCY",
        )

        cases = [
            (100_000.0, 2_000.0, 250_000),
            (95_000.0, 1_900.0, 237_500),
            (110_000.0, 2_200.0, 275_000),
        ]

        for nav, expected_risk_cash, expected_units in cases:
            with self.subTest(nav=nav):
                size = size_shadow_position(
                    account_nav=nav,
                    risk_pct=0.02,
                    entry=1.1000,
                    stop=1.0900,
                    direction="bullish",
                    metadata=metadata,
                    quote_to_account_rate=0.80,
                    max_units=1_000_000,
                )
                self.assertAlmostEqual(size.risk_cash, expected_risk_cash)
                self.assertEqual(size.units, expected_units)
                self.assertEqual(size.signed_units, expected_units)

    def test_global_max_units_caps_eurusd_below_two_percent_risk(self):
        metadata = InstrumentMetadata(
            key="eur_usd",
            name="EUR_USD",
            margin_rate=0.03333333333333,
            minimum_trade_size=1.0,
            trade_units_precision=0,
            pip_location=-4,
            display_precision=5,
            instrument_type="CURRENCY",
        )

        uncapped = size_shadow_position(
            account_nav=100_000.0,
            risk_pct=0.02,
            entry=1.1000,
            stop=1.0900,
            direction="bullish",
            metadata=metadata,
            quote_to_account_rate=0.80,
            max_units=1_000_000,
        )
        capped = size_shadow_position(
            account_nav=100_000.0,
            risk_pct=0.02,
            entry=1.1000,
            stop=1.0900,
            direction="bullish",
            metadata=metadata,
            quote_to_account_rate=0.80,
            max_units=1000,
        )

        self.assertEqual(uncapped.units, 250_000)
        self.assertEqual(capped.units, 1000)
        self.assertLess(capped.units, uncapped.units)

    def test_margin_feasibility_is_nav_scale_invariant(self):
        metadata = InstrumentMetadata(
            key="eur_usd",
            name="EUR_USD",
            margin_rate=0.03333333333333,
            minimum_trade_size=1.0,
            trade_units_precision=0,
            pip_location=-4,
            display_precision=5,
            instrument_type="CURRENCY",
        )

        results = []
        for nav in (100_000.0, 10_000.0, 1_000.0):
            size = size_shadow_position(
                account_nav=nav,
                risk_pct=0.02,
                entry=1.2000,
                stop=1.1990,
                direction="bullish",
                metadata=metadata,
                quote_to_account_rate=1.0,
                max_units=10_000_000,
            )
            feasibility = assess_margin_feasibility(
                account_nav=nav,
                margin_available=nav,
                entry=1.2000,
                metadata=metadata,
                quote_to_account_rate=1.0,
                risk_per_unit=size.risk_per_unit,
                margin_required=size.margin_required,
            )
            results.append(feasibility)

        for result in results:
            self.assertAlmostEqual(result.margin_required_pct_of_nav, results[0].margin_required_pct_of_nav)
            self.assertAlmostEqual(result.max_margin_supported_risk_pct, results[0].max_margin_supported_risk_pct)
        self.assertAlmostEqual(results[0].margin_required_pct_of_nav, 0.8)
        self.assertAlmostEqual(results[0].max_margin_supported_risk_pct, 0.025)

    def test_margin_constrained_full_two_percent_feasible_uses_full_size(self):
        metadata = self._eur_metadata()
        size = size_shadow_position(
            account_nav=100_000.0,
            risk_pct=0.02,
            entry=1.2000,
            stop=1.1990,
            direction="bullish",
            metadata=metadata,
            quote_to_account_rate=1.0,
            max_units=10_000_000,
        )

        constrained = constrain_shadow_position_to_margin(
            account_nav=100_000.0,
            margin_available=100_000.0,
            entry=1.2000,
            direction="bullish",
            metadata=metadata,
            quote_to_account_rate=1.0,
            max_units=10_000_000,
            min_margin_supported_risk_pct=0.004,
            full_size=size,
        )

        self.assertFalse(constrained.margin_fallback_used)
        self.assertTrue(constrained.full_target_risk_feasible)
        self.assertEqual(constrained.final_units, 2_000_000)
        self.assertAlmostEqual(constrained.actual_risk_pct, 0.02)

    def test_margin_fallback_admits_reduced_one_point_eighty_seven_percent(self):
        constrained = self._eur_constrained(margin_available=74_800.0)
        self.assertTrue(constrained.margin_fallback_used)
        self.assertEqual(constrained.final_units, 1_870_000)
        self.assertAlmostEqual(constrained.actual_risk_pct, 0.0187)
        self.assertTrue(constrained.margin_floor_ok)
        self.assertLessEqual(constrained.margin_required, 74_800.0)

    def test_margin_fallback_admits_exact_floor(self):
        constrained = self._eur_constrained(margin_available=16_000.0)
        self.assertTrue(constrained.margin_floor_ok)
        self.assertAlmostEqual(constrained.actual_risk_pct, 0.004)

    def test_margin_fallback_rejects_below_floor(self):
        constrained = self._eur_constrained(margin_available=15_960.0)
        self.assertFalse(constrained.margin_floor_ok)
        self.assertAlmostEqual(constrained.actual_risk_pct, 0.00399)

    def test_margin_fallback_never_exceeds_two_percent(self):
        constrained = self._eur_constrained(margin_available=79_999.0)
        self.assertTrue(constrained.margin_fallback_used)
        self.assertLess(constrained.actual_risk_pct, 0.02)
        self.assertLessEqual(constrained.final_units, constrained.full_size_units)

    def test_margin_fallback_respects_unit_precision(self):
        metadata = InstrumentMetadata(
            key="nasdaq_100",
            name="NAS100_USD",
            margin_rate=0.05,
            minimum_trade_size=0.1,
            trade_units_precision=1,
            pip_location=-1,
            display_precision=1,
            instrument_type="CFD",
        )
        size = size_shadow_position(
            account_nav=100_000.0,
            risk_pct=0.02,
            entry=20_000.0,
            stop=19_900.0,
            direction="bearish",
            metadata=metadata,
            quote_to_account_rate=1.0,
            max_units=1000,
        )
        constrained = constrain_shadow_position_to_margin(
            account_nav=100_000.0,
            margin_available=2_350.0,
            entry=20_000.0,
            direction="bearish",
            metadata=metadata,
            quote_to_account_rate=1.0,
            max_units=1000,
            min_margin_supported_risk_pct=0.001,
            full_size=size,
        )
        self.assertEqual(constrained.final_units, 2.3)
        self.assertEqual(constrained.signed_units, -2.3)

    def test_margin_fallback_rounds_down_under_available_margin(self):
        metadata = self._nas100_metadata()
        size = size_shadow_position(
            account_nav=100_000.0,
            risk_pct=0.02,
            entry=20_000.0,
            stop=19_900.0,
            direction="bullish",
            metadata=metadata,
            quote_to_account_rate=1.0,
            max_units=1000,
        )
        constrained = constrain_shadow_position_to_margin(
            account_nav=100_000.0,
            margin_available=2_399.0,
            entry=20_000.0,
            direction="bullish",
            metadata=metadata,
            quote_to_account_rate=1.0,
            max_units=1000,
            min_margin_supported_risk_pct=0.001,
            full_size=size,
        )
        self.assertEqual(constrained.final_units, 2.3)
        self.assertLessEqual(constrained.margin_required, 2_399.0)
        self.assertGreater(2.4 * 20_000.0 * 0.05, 2_399.0)

    def test_emergency_max_units_still_caps_full_size(self):
        metadata = self._eur_metadata()
        size = size_shadow_position(
            account_nav=100_000.0,
            risk_pct=0.02,
            entry=1.2000,
            stop=1.1990,
            direction="bullish",
            metadata=metadata,
            quote_to_account_rate=1.0,
            max_units=1_000,
        )
        constrained = constrain_shadow_position_to_margin(
            account_nav=100_000.0,
            margin_available=100_000.0,
            entry=1.2000,
            direction="bullish",
            metadata=metadata,
            quote_to_account_rate=1.0,
            max_units=1_000,
            min_margin_supported_risk_pct=0.004,
            full_size=size,
        )
        self.assertEqual(constrained.full_size_units, 1_000)
        self.assertEqual(constrained.final_units, 1_000)
        self.assertAlmostEqual(constrained.actual_risk_pct, 0.00001)

    @staticmethod
    def _eur_metadata() -> InstrumentMetadata:
        return InstrumentMetadata(
            key="eur_usd",
            name="EUR_USD",
            margin_rate=0.03333333333333,
            minimum_trade_size=1.0,
            trade_units_precision=0,
            pip_location=-4,
            display_precision=5,
            instrument_type="CURRENCY",
        )

    @staticmethod
    def _nas100_metadata() -> InstrumentMetadata:
        return InstrumentMetadata(
            key="nasdaq_100",
            name="NAS100_USD",
            margin_rate=0.05,
            minimum_trade_size=0.1,
            trade_units_precision=1,
            pip_location=-1,
            display_precision=1,
            instrument_type="CFD",
        )

    def _eur_constrained(self, *, margin_available: float):
        metadata = self._eur_metadata()
        size = size_shadow_position(
            account_nav=100_000.0,
            risk_pct=0.02,
            entry=1.2000,
            stop=1.1990,
            direction="bullish",
            metadata=metadata,
            quote_to_account_rate=1.0,
            max_units=10_000_000,
        )
        return constrain_shadow_position_to_margin(
            account_nav=100_000.0,
            margin_available=margin_available,
            entry=1.2000,
            direction="bullish",
            metadata=metadata,
            quote_to_account_rate=1.0,
            max_units=10_000_000,
            min_margin_supported_risk_pct=0.004,
            full_size=size,
        )


if __name__ == "__main__":
    unittest.main()
