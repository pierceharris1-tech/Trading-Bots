from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from bots.choch_live.config import ChochConfig
from bots.choch_live.engine import ChochEngine
from bots.choch_live.models import AccountMetadata, InstrumentConfig, InstrumentMetadata
from bots.choch_live.risk import assess_aggregate_risk_admission, current_stop_risk_cash, size_shadow_position
from bots.choch_live.s1_structure import S1Result
from bots.choch_live.state import ChochStateStore
from shared.config import Settings


def config(*, max_units: int = 1_000_000, eur_max_units: int | None = 1_000_000) -> ChochConfig:
    settings = Settings(
        oanda_api_key="k",
        oanda_env="practice",
        base_url="https://api-fxpractice.oanda.com",
        instrument="EUR_USD",
        granularity="M10",
        dry_run=True,
        risk_pct=0.02,
    )
    return ChochConfig(
        bot_id="CHOCH_TEST",
        account_id="101-004-00000000-999",
        expected_account_id="101-004-00000000-999",
        allowed_oanda_env="practice",
        trading_enabled=False,
        env_trading_enabled=False,
        dry_run=True,
        instruments=(
            InstrumentConfig(key="eur_usd", name="EUR_USD", max_units=eur_max_units),
            InstrumentConfig(key="nasdaq_100", name="NAS100_USD", max_units=100),
        ),
        s1_timeframe="H1",
        s2_timeframe="M10",
        risk_pct=0.02,
        max_account_risk_open=0.04,
        max_units=max_units,
        settings=settings,
    )


def eur_metadata() -> InstrumentMetadata:
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


def nas100_metadata() -> InstrumentMetadata:
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


class FakeBroker:
    def __init__(self, *, nav: float = 100_000.0, margin_available: float = 1_000_000.0) -> None:
        self.account_metadata = AccountMetadata(
            account_id="101-004-00000000-999",
            currency="GBP",
            last_transaction_id="42",
        )
        self.instrument_metadata = {"EUR_USD": eur_metadata()}
        self.nav = nav
        self.margin_available = margin_available

    def get_account_risk_state(self):
        return {"nav": self.nav, "margin_available": self.margin_available}

    def get_quote_to_account_rate(self, quote_currency):
        return 0.80


def s1_candidate() -> S1Result:
    ts = pd.Timestamp("2026-01-01T00:00:00Z")
    return S1Result(
        client_order_key="s1-candidate",
        status="filled",
        event_time=ts,
        direction="bullish",
        origin=1.0,
        event_atr=0.01,
        event_extreme=1.11,
        running_extreme=1.11,
        entry_time=ts,
        exit_time=None,
        entry=1.1000,
        stop=1.0900,
        target=1.1150,
        risk=0.0100,
        outcome=None,
        net_r=None,
        cancellation_reason=None,
        payload={},
    )


class TestChochRiskControls(unittest.TestCase):
    def test_candidate_accepted_with_no_existing_exposure(self):
        decision = assess_aggregate_risk_admission(
            nav=100_000.0,
            max_account_risk_open=0.04,
            existing_risk_cash=0.0,
            candidate_intended_risk_cash=2_000.0,
            candidate_actual_risk_cash=2_000.0,
        )
        self.assertTrue(decision.admitted)
        self.assertEqual(decision.aggregate_cap_cash, 4_000.0)

    def test_candidate_accepted_exactly_at_four_percent_cap(self):
        decision = assess_aggregate_risk_admission(
            nav=100_000.0,
            max_account_risk_open=0.04,
            existing_risk_cash=2_000.0,
            candidate_intended_risk_cash=2_000.0,
            candidate_actual_risk_cash=2_000.0,
        )
        self.assertTrue(decision.admitted)
        self.assertEqual(decision.resulting_aggregate_risk_cash, 4_000.0)

    def test_candidate_rejected_when_aggregate_cap_would_be_exceeded(self):
        decision = assess_aggregate_risk_admission(
            nav=100_000.0,
            max_account_risk_open=0.04,
            existing_risk_cash=2_500.0,
            candidate_intended_risk_cash=2_000.0,
            candidate_actual_risk_cash=2_000.0,
        )
        self.assertFalse(decision.admitted)
        self.assertEqual(decision.rejection_reason, "aggregate_risk_cap_exceeded")

    def test_nav_change_reprices_four_percent_cap(self):
        decision = assess_aggregate_risk_admission(
            nav=95_000.0,
            max_account_risk_open=0.04,
            existing_risk_cash=0.0,
            candidate_intended_risk_cash=1_900.0,
            candidate_actual_risk_cash=1_900.0,
        )
        self.assertEqual(decision.aggregate_cap_cash, 3_800.0)

    def test_pending_simulated_s1_orders_reserve_against_shadow_aggregate_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChochStateStore(Path(tmp) / "state.db")
            store.ensure_schema()
            with store.session() as conn:
                for key in ("pending-1", "pending-2"):
                    conn.execute(
                        """
                        INSERT INTO choch_orders (
                            bot_id, account_id, instrument, strategy_leg, client_order_key,
                            state, actual_risk_cash, is_simulated
                        )
                        VALUES ('CHOCH_TEST', '101-004-00000000-999', 'EUR_USD', 'S1', ?,
                                'broker_pending', 2000, 1)
                        """,
                        (key,),
                    )
            self.assertEqual(
                store.aggregate_open_risk_cash(
                    bot_id="CHOCH_TEST",
                    account_id="101-004-00000000-999",
                    is_simulated=True,
                ),
                4_000.0,
            )
            decision = assess_aggregate_risk_admission(
                nav=100_000.0,
                max_account_risk_open=0.04,
                existing_risk_cash=4_000.0,
                candidate_intended_risk_cash=2_000.0,
                candidate_actual_risk_cash=2_000.0,
            )
            self.assertFalse(decision.admitted)

    def test_completed_cancelled_rows_are_not_counted_and_real_simulated_ledgers_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChochStateStore(Path(tmp) / "state.db")
            store.ensure_schema()
            with store.session() as conn:
                conn.execute(
                    """
                    INSERT INTO choch_trades (
                        bot_id, account_id, instrument, strategy_leg, client_order_key,
                        state, actual_risk_cash, is_simulated
                    )
                    VALUES ('CHOCH_TEST', '101-004-00000000-999', 'EUR_USD', 'S1',
                            'sim-open', 'filled', 2000, 1)
                    """
                )
                conn.execute(
                    """
                    INSERT INTO choch_trades (
                        bot_id, account_id, instrument, strategy_leg, client_order_key,
                        state, actual_risk_cash, is_simulated
                    )
                    VALUES ('CHOCH_TEST', '101-004-00000000-999', 'EUR_USD', 'S1',
                            'sim-done', 'tp', 2000, 1)
                    """
                )
                conn.execute(
                    """
                    INSERT INTO choch_orders (
                        bot_id, account_id, instrument, strategy_leg, client_order_key,
                        state, actual_risk_cash, is_simulated
                    )
                    VALUES ('CHOCH_TEST', '101-004-00000000-999', 'EUR_USD', 'S1',
                            'sim-cancelled', 'cancelled', 2000, 1)
                    """
                )
                conn.execute(
                    """
                    INSERT INTO choch_trades (
                        bot_id, account_id, instrument, strategy_leg, client_order_key,
                        state, actual_risk_cash, is_simulated
                    )
                    VALUES ('CHOCH_TEST', '101-004-00000000-999', 'EUR_USD', 'S1',
                            'real-open', 'broker_open', 500, 0)
                    """
                )
            self.assertEqual(
                store.aggregate_open_risk_cash(
                    bot_id="CHOCH_TEST",
                    account_id="101-004-00000000-999",
                    is_simulated=True,
                ),
                2_000.0,
            )
            self.assertEqual(
                store.aggregate_open_risk_cash(
                    bot_id="CHOCH_TEST",
                    account_id="101-004-00000000-999",
                    is_simulated=False,
                ),
                500.0,
            )

    def test_eurusd_normal_sizing_no_longer_hits_old_one_thousand_unit_cap(self):
        size = size_shadow_position(
            account_nav=100_000.0,
            risk_pct=0.02,
            entry=1.1000,
            stop=1.0900,
            direction="bullish",
            metadata=eur_metadata(),
            quote_to_account_rate=0.80,
            max_units=1_000_000,
        )
        self.assertEqual(size.units, 250_000)
        self.assertGreater(size.units, 1000)

    def test_recent_representative_setups_do_not_bind_emergency_caps(self):
        eur = size_shadow_position(
            account_nav=100_000.0,
            risk_pct=0.02,
            entry=1.167211,
            stop=1.167975,
            direction="bearish",
            metadata=eur_metadata(),
            quote_to_account_rate=0.736,
            max_units=10_000_000,
        )
        nas = size_shadow_position(
            account_nav=100_000.0,
            risk_pct=0.02,
            entry=29131.8048,
            stop=29041.5,
            direction="bullish",
            metadata=nas100_metadata(),
            quote_to_account_rate=0.736,
            max_units=100,
        )

        self.assertGreater(10_000_000, eur.units)
        self.assertAlmostEqual(eur.units, 3_556_794)
        self.assertAlmostEqual(eur.actual_risk_cash, 1_999.999493696, places=6)
        self.assertGreater(100, nas.units)
        self.assertAlmostEqual(nas.units, 30.0)
        self.assertAlmostEqual(nas.actual_risk_cash, 1_993.9299840000378)

    def test_per_instrument_emergency_cap_binds_absurd_sizing_result(self):
        size = size_shadow_position(
            account_nav=100_000.0,
            risk_pct=0.02,
            entry=1.1000,
            stop=1.09999,
            direction="bullish",
            metadata=eur_metadata(),
            quote_to_account_rate=0.80,
            max_units=1_000_000,
        )
        self.assertEqual(size.units, 1_000_000)
        self.assertLess(size.actual_risk_cash, size.intended_risk_cash)

    def test_actual_risk_reflects_final_rounded_capped_units(self):
        size = size_shadow_position(
            account_nav=100_000.0,
            risk_pct=0.02,
            entry=1.1000,
            stop=1.0900,
            direction="bullish",
            metadata=eur_metadata(),
            quote_to_account_rate=0.80,
            max_units=249_625,
        )
        self.assertEqual(size.intended_risk_cash, 2_000.0)
        self.assertEqual(size.units, 249_625)
        self.assertAlmostEqual(size.actual_risk_cash, 1_997.0)
        self.assertAlmostEqual(size.actual_risk_pct, 0.01997)

    def test_margin_insufficient_rejects_candidate_without_resizing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChochStateStore(Path(tmp) / "state.db")
            store.ensure_schema()
            engine = ChochEngine(config=config(), broker=FakeBroker(margin_available=1.0), state=store)
            sizing = engine._estimate_shadow_sizing(instrument="EUR_USD", result=s1_candidate())
        self.assertIsNotNone(sizing)
        self.assertFalse(sizing["margin_ok"])
        self.assertFalse(sizing["admitted"])
        self.assertEqual(sizing["rejection_reason"], "margin_insufficient")
        self.assertEqual(sizing["units"], 250_000)

    def test_current_stop_risk_rejects_stale_state_beyond_stop(self):
        with self.assertRaisesRegex(ValueError, "beyond protective stop"):
            current_stop_risk_cash(
                remaining_units=1000,
                current_price=1.0800,
                stop_price=1.0900,
                direction="bullish",
                quote_to_account_rate=0.80,
            )


if __name__ == "__main__":
    unittest.main()
