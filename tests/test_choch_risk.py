from __future__ import annotations

import unittest

from bots.choch_live.models import InstrumentMetadata
from bots.choch_live.risk import quote_currency, size_shadow_position


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


if __name__ == "__main__":
    unittest.main()
