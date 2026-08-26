from __future__ import annotations

import unittest

from bots.choch_live.models import InstrumentMetadata
from bots.choch_live.risk import assess_margin_feasibility, quote_currency, size_shadow_position


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


if __name__ == "__main__":
    unittest.main()
