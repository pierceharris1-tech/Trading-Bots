from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from bots.choch_live.config import ChochConfig
from bots.choch_live.engine import ChochEngine
from bots.choch_live.models import AccountMetadata, InstrumentConfig, InstrumentMetadata
from bots.choch_live.s1_structure import S1Result
from bots.choch_live.state import ChochStateStore
from shared.config import Settings


def execution_config() -> ChochConfig:
    settings = Settings(
        oanda_api_key="k",
        oanda_env="practice",
        base_url="https://api-fxpractice.oanda.com",
        instrument="EUR_USD",
        granularity="M10",
        dry_run=False,
        risk_pct=0.02,
    )
    return ChochConfig(
        bot_id="CHOCH_TEST",
        account_id="101-004-00000000-999",
        expected_account_id="101-004-00000000-999",
        allowed_oanda_env="practice",
        trading_enabled=True,
        env_trading_enabled=True,
        dry_run=False,
        instruments=(InstrumentConfig(key="eur_usd", name="EUR_USD", max_units=10_000_000),),
        s1_timeframe="H1",
        s2_timeframe="M10",
        risk_pct=0.02,
        max_account_risk_open=0.04,
        min_margin_supported_risk_pct=0.004,
        max_units=10_000_000,
        settings=settings,
    )


def s1_result(*, key: str = "s1-key", event_time: str = "2026-01-01T11:00:00Z") -> S1Result:
    ts = pd.Timestamp(event_time)
    return S1Result(
        client_order_key=key,
        status="filled",
        event_time=ts,
        direction="bullish",
        origin=1.09,
        event_atr=0.01,
        event_extreme=1.11,
        running_extreme=1.11,
        entry_time=ts + pd.Timedelta(hours=1),
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


def m10_s2_df(*, periods: int = 11) -> pd.DataFrame:
    index = pd.date_range("2026-01-05 10:00", periods=periods, freq="10min", tz="UTC")
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
    for n, close in enumerate(closes[: max(0, periods - 1)], start=1):
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
    return df


class FakeExecutionBroker:
    def __init__(self) -> None:
        self.account_metadata = AccountMetadata(
            account_id="101-004-00000000-999",
            currency="GBP",
            last_transaction_id="42",
        )
        self.instrument_metadata = {
            "EUR_USD": InstrumentMetadata(
                key="eur_usd",
                name="EUR_USD",
                margin_rate=0.03333333333333,
                minimum_trade_size=1.0,
                trade_units_precision=0,
                pip_location=-4,
                display_precision=5,
                instrument_type="CURRENCY",
            )
        }
        self.submissions: list[dict] = []
        self.replacements: list[dict] = []
        self.cancellations: list[str] = []
        self.market_orders: list[dict] = []
        self.closed_trades: list[str] = []
        self.transactions: list[dict] = []

    def broker_writes_enabled(self):
        return True

    def get_account_risk_state(self):
        return {"nav": 100_000.0, "margin_available": 100_000.0}

    def get_quote_to_account_rate(self, quote_currency):
        return 0.80

    def submit_limit_order_idempotent(self, **kwargs):
        self.submissions.append(kwargs)
        return {"orderCreateTransaction": {"id": str(len(self.submissions))}}

    def replace_order(self, **kwargs):
        self.replacements.append(kwargs)
        return {"orderCreateTransaction": {"id": f"replacement-{len(self.replacements)}"}}

    def cancel_order(self, order_id):
        self.cancellations.append(order_id)
        return {"orderCancelTransaction": {"id": f"cancel-{len(self.cancellations)}", "orderID": order_id}}

    def submit_market_order_idempotent(self, **kwargs):
        self.market_orders.append(kwargs)
        return {"orderCreateTransaction": {"id": f"market-{len(self.market_orders)}"}}

    def close_trade(self, trade_id):
        self.closed_trades.append(trade_id)
        return {
            "orderFillTransaction": {
                "id": f"close-{len(self.closed_trades)}",
                "time": "2026-01-01T13:30:00Z",
                "pl": "12.5",
                "tradeClosed": {"tradeID": trade_id},
            }
        }

    def get_transactions_since(self, transaction_id):
        return {"changes": {"transactions": self.transactions}}

    def get_pending_orders(self):
        return {"orders": []}

    def get_open_trades(self):
        return {"trades": []}


class TestChochExecution(unittest.TestCase):
    def test_historical_replay_signal_cannot_submit_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = ChochStateStore(Path(tmp) / "state.db")
            state.ensure_schema()
            state.set_execution_watermark(
                bot_id="CHOCH_TEST",
                account_id="101-004-00000000-999",
                instrument="EUR_USD",
                granularity="H1",
                candle_time="2026-01-01T12:00:00+00:00",
            )
            broker = FakeExecutionBroker()
            engine = ChochEngine(config=execution_config(), broker=broker, state=state)

            engine._maybe_execute_s1(instrument="EUR_USD", result=s1_result(event_time="2026-01-01T11:00:00Z"))

            self.assertEqual(broker.submissions, [])

    def test_genuinely_new_post_watermark_s1_can_submit_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = ChochStateStore(Path(tmp) / "state.db")
            state.ensure_schema()
            state.set_execution_watermark(
                bot_id="CHOCH_TEST",
                account_id="101-004-00000000-999",
                instrument="EUR_USD",
                granularity="H1",
                candle_time="2026-01-01T10:00:00+00:00",
            )
            broker = FakeExecutionBroker()
            engine = ChochEngine(config=execution_config(), broker=broker, state=state)
            result = s1_result(event_time="2026-01-01T11:00:00Z")
            engine._reconciliation_ok = True

            with patch.dict("os.environ", {"CHOCH_TRADING_ENABLED": "1"}, clear=False):
                engine._maybe_execute_s1(instrument="EUR_USD", result=result)
                engine._maybe_execute_s1(instrument="EUR_USD", result=result)

            self.assertEqual(len(broker.submissions), 1)
            order = state.get_real_order_by_client_key(
                bot_id="CHOCH_TEST",
                account_id="101-004-00000000-999",
                client_order_key=result.client_order_key,
            )
            self.assertIsNotNone(order)
            assert order is not None
            self.assertEqual(order["state"], "broker_pending")
            self.assertEqual(order["oanda_order_id"], "1")
            self.assertEqual(order["is_simulated"], 0)

    def test_watermark_equal_signal_is_current_and_can_submit(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = ChochStateStore(Path(tmp) / "state.db")
            state.ensure_schema()
            broker = FakeExecutionBroker()
            engine = ChochEngine(config=execution_config(), broker=broker, state=state)
            latest = pd.Timestamp("2026-01-01T12:00:00Z")
            payload = {
                "candles": [
                    {
                        "complete": True,
                        "time": latest.isoformat().replace("+00:00", "Z"),
                        "bid": {"o": "1.1", "h": "1.2", "l": "1.0", "c": "1.1"},
                        "ask": {"o": "1.1", "h": "1.2", "l": "1.0", "c": "1.1"},
                    }
                ]
            }
            broker.get_complete_candles = lambda **kwargs: (latest, payload["candles"][0], payload)

            engine._process_timeframe(instrument="EUR_USD", granularity="H1", count=500)
            engine._reconciliation_ok = True
            engine._maybe_execute_s1(instrument="EUR_USD", result=s1_result(event_time="2026-01-01T12:00:00Z"))

            self.assertEqual(len(broker.submissions), 1)

    def test_signal_before_watermark_is_replay_and_cannot_submit(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = ChochStateStore(Path(tmp) / "state.db")
            state.ensure_schema()
            state.set_execution_watermark(
                bot_id="CHOCH_TEST",
                account_id="101-004-00000000-999",
                instrument="EUR_USD",
                granularity="H1",
                candle_time="2026-01-01T12:00:00+00:00",
            )
            broker = FakeExecutionBroker()
            engine = ChochEngine(config=execution_config(), broker=broker, state=state)
            engine._reconciliation_ok = True

            engine._maybe_execute_s1(instrument="EUR_USD", result=s1_result(event_time="2026-01-01T11:00:00Z"))

            self.assertEqual(broker.submissions, [])

    def test_simulated_exposure_does_not_affect_real_execution_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = ChochStateStore(Path(tmp) / "state.db")
            state.ensure_schema()
            with state.session() as conn:
                conn.execute(
                    """
                    INSERT INTO choch_trades (
                        bot_id, account_id, instrument, strategy_leg, client_order_key,
                        state, actual_risk_cash, is_simulated
                    )
                    VALUES ('CHOCH_TEST', '101-004-00000000-999', 'EUR_USD', 'S1',
                            'sim-risk', 'filled', 4000, 1)
                    """
                )
            state.set_execution_watermark(
                bot_id="CHOCH_TEST",
                account_id="101-004-00000000-999",
                instrument="EUR_USD",
                granularity="H1",
                candle_time="2026-01-01T10:00:00+00:00",
            )
            broker = FakeExecutionBroker()
            engine = ChochEngine(config=execution_config(), broker=broker, state=state)
            engine._reconciliation_ok = True

            engine._maybe_execute_s1(instrument="EUR_USD", result=s1_result(event_time="2026-01-01T11:00:00Z"))

            self.assertEqual(len(broker.submissions), 1)

    def test_dynamic_anchor_change_replaces_existing_pending_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = ChochStateStore(Path(tmp) / "state.db")
            state.ensure_schema()
            state.set_execution_watermark(
                bot_id="CHOCH_TEST",
                account_id="101-004-00000000-999",
                instrument="EUR_USD",
                granularity="H1",
                candle_time="2026-01-01T10:00:00+00:00",
            )
            state.upsert_real_order(
                bot_id="CHOCH_TEST",
                account_id="101-004-00000000-999",
                instrument="EUR_USD",
                strategy_leg="S1",
                client_order_key="s1-key",
                oanda_order_id="old-order",
                state="broker_pending",
                order_type="LIMIT",
                price=1.1010,
                stop_price=1.0900,
                units=250_000,
                signed_units=250_000,
                intended_risk_cash=2_000.0,
                actual_risk_cash=2_000.0,
                actual_risk_pct=0.02,
                margin_required=7_000.0,
            )
            broker = FakeExecutionBroker()
            engine = ChochEngine(config=execution_config(), broker=broker, state=state)
            engine._reconciliation_ok = True

            engine._maybe_execute_s1(instrument="EUR_USD", result=s1_result(event_time="2026-01-01T11:00:00Z"))

            self.assertEqual(len(broker.replacements), 1)
            self.assertEqual(broker.replacements[0]["order_id"], "old-order")
            order = state.get_real_order_by_client_key(
                bot_id="CHOCH_TEST",
                account_id="101-004-00000000-999",
                client_order_key="s1-key",
            )
            self.assertEqual(order["oanda_order_id"], "replacement-1")
            self.assertAlmostEqual(order["price"], 1.1)

    def test_invalidated_setup_cancels_existing_real_pending_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = ChochStateStore(Path(tmp) / "state.db")
            state.ensure_schema()
            state.set_execution_watermark(
                bot_id="CHOCH_TEST",
                account_id="101-004-00000000-999",
                instrument="EUR_USD",
                granularity="H1",
                candle_time="2026-01-01T10:00:00+00:00",
            )
            state.upsert_real_order(
                bot_id="CHOCH_TEST",
                account_id="101-004-00000000-999",
                instrument="EUR_USD",
                strategy_leg="S1",
                client_order_key="s1-key",
                oanda_order_id="pending-order",
                state="broker_pending",
                order_type="LIMIT",
                price=1.1000,
                stop_price=1.0900,
                units=250_000,
                signed_units=250_000,
                intended_risk_cash=2_000.0,
                actual_risk_cash=2_000.0,
                actual_risk_pct=0.02,
                margin_required=7_000.0,
            )
            broker = FakeExecutionBroker()
            engine = ChochEngine(config=execution_config(), broker=broker, state=state)
            engine._reconciliation_ok = True
            cancelled = s1_result(event_time="2026-01-01T11:00:00Z")
            cancelled = S1Result(
                client_order_key=cancelled.client_order_key,
                status="cancel_new_choch",
                event_time=cancelled.event_time,
                direction=cancelled.direction,
                origin=cancelled.origin,
                event_atr=cancelled.event_atr,
                event_extreme=cancelled.event_extreme,
                running_extreme=cancelled.running_extreme,
                entry_time=None,
                exit_time=None,
                entry=None,
                stop=cancelled.stop,
                target=None,
                risk=None,
                outcome=None,
                net_r=None,
                cancellation_reason="cancel_new_choch",
                payload={},
            )

            engine._maybe_cancel_s1(instrument="EUR_USD", result=cancelled)

            self.assertEqual(broker.cancellations, ["pending-order"])
            order = state.get_real_order_by_client_key(
                bot_id="CHOCH_TEST",
                account_id="101-004-00000000-999",
                client_order_key="s1-key",
            )
            self.assertEqual(order["state"], "cancelled")
            self.assertEqual(order["cancel_transaction_id"], "cancel-1")

    def test_s1_order_fill_transaction_creates_real_open_trade(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = ChochStateStore(Path(tmp) / "state.db")
            state.ensure_schema()
            state.upsert_real_order(
                bot_id="CHOCH_TEST",
                account_id="101-004-00000000-999",
                instrument="EUR_USD",
                strategy_leg="S1",
                client_order_key="s1-key",
                oanda_order_id="order-1",
                state="broker_pending",
                order_type="LIMIT",
                price=1.1000,
                stop_price=1.0900,
                units=250_000,
                signed_units=250_000,
                intended_risk_cash=2_000.0,
                actual_risk_cash=2_000.0,
                actual_risk_pct=0.02,
                margin_required=7_000.0,
            )
            engine = ChochEngine(config=execution_config(), broker=FakeExecutionBroker(), state=state)

            engine._process_transaction(
                {
                    "id": "43",
                    "type": "ORDER_FILL",
                    "time": "2026-01-01T12:00:00Z",
                    "orderID": "order-1",
                    "price": "1.1001",
                    "units": "250000",
                    "tradeOpened": {"tradeID": "trade-1", "clientExtensions": {"id": "s1-key"}},
                }
            )

            order = state.get_real_order_by_client_key(
                bot_id="CHOCH_TEST", account_id="101-004-00000000-999", client_order_key="s1-key"
            )
            trade = state.get_real_trade_by_client_key(
                bot_id="CHOCH_TEST", account_id="101-004-00000000-999", client_order_key="s1-key"
            )
            self.assertEqual(order["state"], "filled")
            self.assertEqual(order["fill_transaction_id"], "43")
            self.assertEqual(trade["state"], "broker_open")
            self.assertEqual(trade["oanda_trade_id"], "trade-1")
            self.assertAlmostEqual(trade["entry_price"], 1.1001)

    def test_s1_tp_transaction_closes_trade_and_arms_s2(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = ChochStateStore(Path(tmp) / "state.db")
            state.ensure_schema()
            state.upsert_real_trade(
                bot_id="CHOCH_TEST",
                account_id="101-004-00000000-999",
                instrument="EUR_USD",
                strategy_leg="S1",
                client_order_key="s1-key",
                oanda_trade_id="trade-1",
                state="broker_open",
                entry_time="2026-01-01T12:00:00Z",
                entry_price=1.1000,
                stop_price=1.0900,
                tp_price=1.1150,
                units=250_000,
                signed_units=250_000,
                intended_risk_cash=2_000.0,
                actual_risk_cash=2_000.0,
                actual_risk_pct=0.02,
                margin_required=7_000.0,
                fill_transaction_id="43",
            )
            engine = ChochEngine(config=execution_config(), broker=FakeExecutionBroker(), state=state)

            engine._process_transaction(
                {
                    "id": "44",
                    "type": "ORDER_FILL",
                    "reason": "TAKE_PROFIT_ORDER",
                    "time": "2026-01-01T13:00:00Z",
                    "price": "1.1150",
                    "pl": "3000.0",
                    "tradesClosed": [{"tradeID": "trade-1", "realizedPL": "3000.0"}],
                }
            )

            trade = state.get_real_trade_by_client_key(
                bot_id="CHOCH_TEST", account_id="101-004-00000000-999", client_order_key="s1-key"
            )
            self.assertEqual(trade["state"], "tp")
            self.assertEqual(trade["close_transaction_id"], "44")
            self.assertAlmostEqual(trade["realized_r"], 1.5)
            s2 = state.observing_real_s2_setups(
                bot_id="CHOCH_TEST", account_id="101-004-00000000-999", instrument="EUR_USD"
            )
            self.assertEqual(len(s2), 1)
            self.assertEqual(s2[0]["client_order_key"], "s1-key-S2")
            self.assertEqual(s2[0]["s1_tp_time"], "2026-01-01T13:00:00Z")
            self.assertEqual(pd.Timestamp(s2[0]["expected_decision_time"]), pd.Timestamp("2026-01-01T14:40:00Z"))

    def test_s1_sl_transaction_does_not_arm_s2(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = ChochStateStore(Path(tmp) / "state.db")
            state.ensure_schema()
            state.upsert_real_trade(
                bot_id="CHOCH_TEST",
                account_id="101-004-00000000-999",
                instrument="EUR_USD",
                strategy_leg="S1",
                client_order_key="s1-key",
                oanda_trade_id="trade-1",
                state="broker_open",
                entry_time="2026-01-01T12:00:00Z",
                entry_price=1.1000,
                stop_price=1.0900,
                tp_price=1.1150,
                units=250_000,
                signed_units=250_000,
                intended_risk_cash=2_000.0,
                actual_risk_cash=2_000.0,
                actual_risk_pct=0.02,
                margin_required=7_000.0,
                fill_transaction_id="43",
            )
            engine = ChochEngine(config=execution_config(), broker=FakeExecutionBroker(), state=state)

            engine._process_transaction(
                {
                    "id": "44",
                    "type": "ORDER_FILL",
                    "reason": "STOP_LOSS_ORDER",
                    "time": "2026-01-01T13:00:00Z",
                    "pl": "-2000.0",
                    "tradesClosed": [{"tradeID": "trade-1", "realizedPL": "-2000.0"}],
                }
            )

            trade = state.get_real_trade_by_client_key(
                bot_id="CHOCH_TEST", account_id="101-004-00000000-999", client_order_key="s1-key"
            )
            self.assertEqual(trade["state"], "sl")
            self.assertEqual(
                state.observing_real_s2_setups(
                    bot_id="CHOCH_TEST", account_id="101-004-00000000-999", instrument="EUR_USD"
                ),
                [],
            )

    def test_unrelated_oanda_transaction_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = ChochStateStore(Path(tmp) / "state.db")
            state.ensure_schema()
            engine = ChochEngine(config=execution_config(), broker=FakeExecutionBroker(), state=state)
            engine._process_transaction(
                {
                    "id": "55",
                    "type": "ORDER_FILL",
                    "orderID": "manual",
                    "tradeOpened": {"tradeID": "manual-trade", "clientExtensions": {"id": "manual"}},
                }
            )
            self.assertEqual(state.active_real_trades(bot_id="CHOCH_TEST", account_id="101-004-00000000-999"), [])

    def test_duplicate_fill_transaction_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = ChochStateStore(Path(tmp) / "state.db")
            state.ensure_schema()
            state.upsert_real_order(
                bot_id="CHOCH_TEST",
                account_id="101-004-00000000-999",
                instrument="EUR_USD",
                strategy_leg="S1",
                client_order_key="s1-key",
                oanda_order_id="order-1",
                state="broker_pending",
                order_type="LIMIT",
                price=1.1000,
                stop_price=1.0900,
                units=250_000,
                signed_units=250_000,
                intended_risk_cash=2_000.0,
                actual_risk_cash=2_000.0,
                actual_risk_pct=0.02,
                margin_required=7_000.0,
            )
            engine = ChochEngine(config=execution_config(), broker=FakeExecutionBroker(), state=state)
            tx = {
                "id": "43",
                "type": "ORDER_FILL",
                "time": "2026-01-01T12:00:00Z",
                "orderID": "order-1",
                "price": "1.1001",
                "units": "250000",
                "tradeOpened": {"tradeID": "trade-1", "clientExtensions": {"id": "s1-key"}},
            }
            engine._process_transaction(tx)
            engine._process_transaction(tx)
            self.assertEqual(len(state.active_real_trades(bot_id="CHOCH_TEST", account_id="101-004-00000000-999")), 1)

    def test_pending_to_filled_risk_not_double_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = ChochStateStore(Path(tmp) / "state.db")
            state.ensure_schema()
            state.upsert_real_order(
                bot_id="CHOCH_TEST",
                account_id="101-004-00000000-999",
                instrument="EUR_USD",
                strategy_leg="S1",
                client_order_key="s1-key",
                oanda_order_id="order-1",
                state="broker_pending",
                order_type="LIMIT",
                price=1.1000,
                stop_price=1.0900,
                units=250_000,
                signed_units=250_000,
                intended_risk_cash=2_000.0,
                actual_risk_cash=2_000.0,
                actual_risk_pct=0.02,
                margin_required=7_000.0,
            )
            self.assertAlmostEqual(
                state.aggregate_open_risk_cash(bot_id="CHOCH_TEST", account_id="101-004-00000000-999", is_simulated=False),
                2_000.0,
            )

    def test_transaction_watermark_persisted_after_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = ChochStateStore(Path(tmp) / "state.db")
            state.ensure_schema()
            broker = FakeExecutionBroker()
            broker.transactions = [{"id": "43", "type": "ORDER_CREATE", "clientExtensions": {"id": "manual"}}]
            engine = ChochEngine(config=execution_config(), broker=broker, state=state)

            result = engine.reconcile_read_only()

            self.assertEqual(result["status"], "ok")
            row = state.get_reconciliation(bot_id="CHOCH_TEST", account_id="101-004-00000000-999")
            self.assertEqual(row["last_transaction_id"], "43")

    def test_s2_exact_tenth_completed_m10_submits_opposite_market_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = ChochStateStore(Path(tmp) / "state.db")
            state.ensure_schema()
            state.upsert_real_s2_observation(
                bot_id="CHOCH_TEST",
                account_id="101-004-00000000-999",
                instrument="EUR_USD",
                client_order_key="s1-key-S2",
                parent_s1_trade_id="trade-1",
                direction="bearish",
                s1_tp_time="2026-01-05T10:00:00Z",
                observation_start_time="2026-01-05T10:00:00Z",
                expected_decision_time="2026-01-05T11:40:00Z",
                entry=100.0,
                stop=90.0,
                tp=115.0,
                r_value=10.0,
            )
            broker = FakeExecutionBroker()
            engine = ChochEngine(config=execution_config(), broker=broker, state=state)
            engine._reconciliation_ok = True
            engine._latest_complete_candle[("EUR_USD", "M10")] = pd.Timestamp("2026-01-05T11:40:00Z")

            engine._process_real_s2_observations(instrument="EUR_USD", s2_df=m10_s2_df(periods=11))

            self.assertEqual(len(broker.market_orders), 1)
            self.assertEqual(broker.market_orders[0]["client_order_id"], "s1-key-S2")
            self.assertLess(broker.market_orders[0]["signed_units"], 0)
            order = state.get_real_order_by_client_key(
                bot_id="CHOCH_TEST", account_id="101-004-00000000-999", client_order_key="s1-key-S2"
            )
            self.assertEqual(order["order_type"], "MARKET")

    def test_s2_missed_downtime_does_not_enter_retroactively(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = ChochStateStore(Path(tmp) / "state.db")
            state.ensure_schema()
            state.upsert_real_s2_observation(
                bot_id="CHOCH_TEST",
                account_id="101-004-00000000-999",
                instrument="EUR_USD",
                client_order_key="s1-key-S2",
                parent_s1_trade_id="trade-1",
                direction="bearish",
                s1_tp_time="2026-01-05T10:00:00Z",
                observation_start_time="2026-01-05T10:00:00Z",
                expected_decision_time="2026-01-05T11:40:00Z",
                entry=100.0,
                stop=90.0,
                tp=115.0,
                r_value=10.0,
            )
            broker = FakeExecutionBroker()
            engine = ChochEngine(config=execution_config(), broker=broker, state=state)
            engine._reconciliation_ok = True
            engine._latest_complete_candle[("EUR_USD", "M10")] = pd.Timestamp("2026-01-05T11:50:00Z")

            engine._process_real_s2_observations(instrument="EUR_USD", s2_df=m10_s2_df(periods=12))

            self.assertEqual(broker.market_orders, [])
            s2 = state.observing_real_s2_setups(
                bot_id="CHOCH_TEST", account_id="101-004-00000000-999", instrument="EUR_USD"
            )
            self.assertEqual(s2, [])

    def test_unprotected_open_trade_blocks_new_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = ChochStateStore(Path(tmp) / "state.db")
            state.ensure_schema()
            state.upsert_real_trade(
                bot_id="CHOCH_TEST",
                account_id="101-004-00000000-999",
                instrument="EUR_USD",
                strategy_leg="S1",
                client_order_key="open-key",
                oanda_trade_id="trade-1",
                state="broker_open",
                entry_time="2026-01-01T12:00:00Z",
                entry_price=1.1000,
                stop_price=1.0900,
                tp_price=1.1150,
                units=250_000,
                signed_units=250_000,
                intended_risk_cash=2_000.0,
                actual_risk_cash=2_000.0,
                actual_risk_pct=0.02,
                margin_required=7_000.0,
                fill_transaction_id="43",
            )
            broker = FakeExecutionBroker()
            broker.get_open_trades = lambda: {"trades": [{"id": "trade-1", "instrument": "EUR_USD"}]}
            engine = ChochEngine(config=execution_config(), broker=broker, state=state)

            result = engine.reconcile_read_only()

            self.assertEqual(result["status"], "mismatch")
            self.assertIn("CHOCH_UNPROTECTED_TRADE", result["mismatches"])
            engine._maybe_execute_s1(instrument="EUR_USD", result=s1_result(key="new-key", event_time="2026-01-01T11:00:00Z"))
            self.assertEqual(broker.submissions, [])
            engine = ChochEngine(config=execution_config(), broker=FakeExecutionBroker(), state=state)
            engine._process_transaction(
                {
                    "id": "43",
                    "type": "ORDER_FILL",
                    "time": "2026-01-01T12:00:00Z",
                    "orderID": "order-1",
                    "price": "1.1001",
                    "units": "250000",
                    "tradeOpened": {"tradeID": "trade-1", "clientExtensions": {"id": "s1-key"}},
                }
            )
            self.assertAlmostEqual(
                state.aggregate_open_risk_cash(bot_id="CHOCH_TEST", account_id="101-004-00000000-999", is_simulated=False),
                2_000.0,
            )


if __name__ == "__main__":
    unittest.main()
