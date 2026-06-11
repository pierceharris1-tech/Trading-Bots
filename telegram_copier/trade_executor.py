"""
trade_executor.py
Handles all OANDA interactions:
- Position sizing (1% risk split across TPs)
- Placing market and limit orders
- Modifying SL (move to BE)
- Full closes
- Cancel/move pending limit orders
"""

import logging
import math
from typing import Optional
import sys
import os

sys.path.insert(0, "/home/ubuntu/trading-bots")
from shared.oanda_client import OandaClient
from instrument_map import resolve_instrument, MIN_UNITS
import position_manager as pm

logger = logging.getLogger(__name__)

RISK_PER_SIGNAL = 0.01   # 1% of NAV per full signal

# ── Position sizing ────────────────────────────────────────────────────────────

def calculate_units(client: OandaClient, instrument: str, direction: str,
                    entry_price: float, sl: float, risk_fraction: float) -> int:
    nav = client.get_account_nav()
    risk_amount = nav * risk_fraction

    sl_distance = abs(entry_price - sl)
    if sl_distance == 0:
        logger.warning("SL distance is zero")
        return 0

    price_data = client.get_price(instrument)
    if not price_data:
        logger.warning(f"Could not get price for {instrument}")
        return 0

    quote_currency = _get_quote_currency(instrument)
    pl_per_unit_gbp = _convert_to_gbp(client, sl_distance, quote_currency)

    if pl_per_unit_gbp <= 0:
        return 0

    units = math.floor(risk_amount / pl_per_unit_gbp)
    min_u = MIN_UNITS.get(instrument, 1)

    if units < min_u:
        logger.warning(f"Calculated units {units} below minimum {min_u} for {instrument}")
        return 0

    return units


def _get_quote_currency(instrument: str) -> str:
    parts = instrument.split("_")
    return parts[1] if len(parts) == 2 else "USD"


def _convert_to_gbp(client: OandaClient, amount: float, from_currency: str) -> float:
    if from_currency == "GBP":
        return amount
    pair_map = {
        "USD": "GBP_USD",
        "EUR": "GBP_EUR",
        "JPY": "GBP_JPY",
    }
    pair = pair_map.get(from_currency, "GBP_USD")
    price = client.get_price(pair)
    if price:
        mid = (price["ask"] + price["bid"]) / 2
        return amount / mid
    return amount


# ── Entry execution ────────────────────────────────────────────────────────────

def execute_entry(client: OandaClient, signal, telegram_msg_id: int):
    instrument = resolve_instrument(signal.instrument_raw)
    if not instrument:
        logger.error(f"Unknown instrument: {signal.instrument_raw}")
        return

    price_data = client.get_price(instrument)
    if not price_data:
        logger.error(f"Cannot get price for {instrument}")
        return

    entry_price = signal.entry_price or (
        price_data["ask"] if signal.direction == "buy" else price_data["bid"]
    )

    tps = [("tp1", signal.tp1), ("tp2", signal.tp2), ("tp3", signal.tp3)]
    risk_per_leg = RISK_PER_SIGNAL / 3  # 0.333% each

    placed = 0
    for tp_label, tp_price in tps:
        units = calculate_units(
            client, instrument, signal.direction,
            entry_price, signal.sl, risk_per_leg
        )
        if units == 0:
            if placed == 0:
                logger.error(f"Could not size even TP1 trade for {instrument} — aborting")
                return
            logger.info(f"Skipping {tp_label} — insufficient margin")
            break

        trade_id = _place_order(
            client, instrument, signal.direction, signal.order_type,
            entry_price, signal.sl, tp_price, units
        )

        if trade_id:
            pm.record_trade(
                telegram_msg_id=telegram_msg_id,
                oanda_trade_id=trade_id,
                instrument=instrument,
                direction=signal.direction,
                entry_price=entry_price,
                sl=signal.sl,
                tp=tp_price,
                tp_label=tp_label,
                units=units
            )
            placed += 1
            logger.info(f"Placed {tp_label} | {instrument} {signal.direction} "
                        f"{units} units | TP={tp_price} | trade_id={trade_id}")
        else:
            logger.warning(f"Failed to place {tp_label} — stopping")
            break

    logger.info(f"Signal executed: {placed}/3 legs for {instrument}")


def _place_order(client: OandaClient, instrument: str, direction: str,
                 order_type: str, entry_price: float, sl: float,
                 tp: Optional[float], units: int) -> Optional[str]:
    signed_units = units if direction == "buy" else -units

    try:
        if order_type == "market":
            resp = client.place_market_order(
                units=signed_units,
                instrument=instrument,
                stop_loss=sl,
                take_profit=tp,
            )
        else:
            resp = client.place_limit_order(
                units=signed_units,
                instrument=instrument,
                price=entry_price,
                stop_loss=sl,
                take_profit=tp,
            )

        if "orderFillTransaction" in resp:
            return resp["orderFillTransaction"]["tradeOpened"]["tradeID"]
        elif "orderCreateTransaction" in resp:
            return resp["orderCreateTransaction"]["id"]
        else:
            logger.error(f"Unexpected order response: {resp}")
            return None

    except Exception as e:
        logger.error(f"Order placement error: {e}")
        return None


# ── Management execution ───────────────────────────────────────────────────────

def execute_management(client: OandaClient, mgmt, telegram_msg_id: int):
    """Handle sl_to_be, close_all, close_instrument."""
    instrument = resolve_instrument(mgmt.instrument_raw) if mgmt.instrument_raw else None
    action = mgmt.action

    # Determine which trades this applies to
    if mgmt.reply_to_msg_id:
        trades = pm.get_open_trades_for_message(mgmt.reply_to_msg_id)
        logger.info(f"Management via reply → {len(trades)} trades from msg {mgmt.reply_to_msg_id}")
    elif instrument:
        trades = pm.get_open_trades_for_instrument(instrument)
        logger.info(f"Management for {instrument} → {len(trades)} trades")
    else:
        trades = pm.get_all_open_trades()
        logger.info(f"Management (all instruments) → {len(trades)} trades")

    if not trades:
        logger.info("No matching open trades for management signal")
        return

    for trade in trades:
        trade_id = trade["oanda_trade_id"]

        if action == "sl_to_be":
            _move_sl_to_be(client, trade_id, trade["entry_price"])

        elif action in ("close_all", "close_instrument"):
            _close_trade(client, trade_id)
            pm.mark_closed(trade_id)


def _move_sl_to_be(client: OandaClient, trade_id: str, entry_price: float):
    try:
        client.modify_trade(trade_id, stop_loss=entry_price)
        logger.info(f"SL moved to BE ({entry_price}) for trade {trade_id}")
    except Exception as e:
        logger.error(f"SL to BE failed for {trade_id}: {e}")


def _close_trade(client: OandaClient, trade_id: str):
    try:
        client.close_trade(trade_id)
        logger.info(f"Closed trade {trade_id}")
    except Exception as e:
        logger.error(f"Close failed for {trade_id}: {e}")


# ── Cancel / Move pending orders ───────────────────────────────────────────────

def cancel_pending_order(client: OandaClient, instrument: Optional[str],
                         entry_price_ref: Optional[float]):
    """Cancel a specific pending limit order by instrument and approximate entry price."""
    try:
        resp = client.list_pending_orders()
        orders = resp.get("orders", [])

        for order in orders:
            if order.get("type") not in ("LIMIT", "STOP"):
                continue
            instr_match = (instrument is None or order.get("instrument") == instrument)
            price_match = (entry_price_ref is None or
                          abs(float(order.get("price", 0)) - entry_price_ref) < 2)
            if instr_match and price_match:
                client.cancel_order(order["id"])
                logger.info(f"Cancelled order {order['id']} | "
                            f"{order.get('instrument')} @ {order.get('price')}")
                return

        logger.warning(f"No matching pending order found to cancel | "
                      f"instrument={instrument} price_ref={entry_price_ref}")
    except Exception as e:
        logger.error(f"Cancel order error: {e}")


def cancel_all_pending_orders(client: OandaClient):
    """Cancel all pending limit/stop orders."""
    try:
        resp = client.list_pending_orders()
        orders = resp.get("orders", [])
        cancelled = 0
        for order in orders:
            if order.get("type") in ("LIMIT", "STOP"):
                client.cancel_order(order["id"])
                cancelled += 1
                logger.info(f"Cancelled order {order['id']} | "
                            f"{order.get('instrument')} @ {order.get('price')}")
        logger.info(f"Cancelled {cancelled} pending orders total")
    except Exception as e:
        logger.error(f"Cancel all orders error: {e}")


def move_pending_order(client: OandaClient, instrument: Optional[str],
                       entry_price_ref: Optional[float], new_price: Optional[float]):
    """Cancel existing pending order and replace at new price."""
    if new_price is None:
        logger.warning("move_pending_order called with no new_price")
        return
    try:
        resp = client.list_pending_orders()
        orders = resp.get("orders", [])

        for order in orders:
            if order.get("type") not in ("LIMIT", "STOP"):
                continue
            instr_match = (instrument is None or order.get("instrument") == instrument)
            price_match = (entry_price_ref is None or
                          abs(float(order.get("price", 0)) - entry_price_ref) < 2)
            if instr_match and price_match:
                # Cancel and recreate at new price
                client.cancel_order(order["id"])
                logger.info(f"Cancelled order {order['id']} to move price")

                # Recreate with same parameters at new price
                units = int(order.get("units", 1))
                sl = float(order["stopLossOnFill"]["price"]) if order.get("stopLossOnFill") else None
                tp = float(order["takeProfitOnFill"]["price"]) if order.get("takeProfitOnFill") else None
                direction = "buy" if units > 0 else "sell"

                new_resp = client.place_limit_order(
                    units=units,
                    instrument=order["instrument"],
                    price=new_price,
                    stop_loss=sl,
                    take_profit=tp,
                )
                new_id = new_resp.get("orderCreateTransaction", {}).get("id")
                logger.info(f"Replaced order at new price {new_price} | new_id={new_id}")
                return

        logger.warning(f"No matching order found to move")
    except Exception as e:
        logger.error(f"Move order error: {e}")
