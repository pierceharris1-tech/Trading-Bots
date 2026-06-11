from __future__ import annotations

import requests
from typing import Any, Optional

from shared.logging_utils import get_logger

log = get_logger("shared.oanda_client")

from decimal import Decimal, ROUND_HALF_UP


def build_order_tag(bot_id: str, timeframe: str) -> str:
    bot = str(bot_id or "UNKNOWN_BOT").strip()
    tf = str(timeframe or "UNKNOWN_TF").strip().upper()
    return f"{bot}|{tf}"


class OandaClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        account_id: str,
        bot_id: str = "UNKNOWN_BOT",
        timeframe: str = "UNKNOWN_TF",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.account_id = account_id
        self.bot_id = bot_id
        self.order_tag = build_order_tag(bot_id=bot_id, timeframe=timeframe)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )
        # Cache instrument -> displayPrecision (int)
        self._precision_cache: dict[str, int] = {}


    def _order_extensions(self) -> dict[str, str]:
        return {"tag": self.order_tag}

    def _get_display_precision(self, instrument: str) -> int:
        """
        Fetch and cache OANDA displayPrecision for an instrument.
        Used to format TP/SL/price strings to avoid precision rejection.
        """
        inst = str(instrument).strip()
        if inst in self._precision_cache:
            return self._precision_cache[inst]

        data = self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/instruments",
            context={
                "account_id": self.account_id,
                "instrument": inst,
                "order_type": "NONE",
                "units": 0,
            },
        )
        instruments = data.get("instruments") or []
        match = next((x for x in instruments if x.get("name") == inst), None)
        if not match:
            prec = 1
        else:
            prec = int(match.get("displayPrecision", 1))

        self._precision_cache[inst] = prec
        return prec

    def _fmt_price(self, instrument: str, price: float) -> str:
        """
        Format a price as a string with the instrument's displayPrecision,
        avoiding float artifacts (e.g. 10967.699999999997).
        """
        prec = self._get_display_precision(instrument)
        q = Decimal("1") if prec <= 0 else Decimal("1").scaleb(-prec)  # 10^-prec
        d = Decimal(str(price)).quantize(q, rounding=ROUND_HALF_UP)
        return format(d, "f")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        r = self.session.request(method, url, params=params, json=payload, timeout=20)
        if not r.ok:
            try:
                body = r.json()
            except Exception:
                body = r.text
            merged_ctx = {"bot_id": self.bot_id}
            merged_ctx.update(context or {})
            log.error(
                "OANDA_HTTP_ERROR | status=%s | context=%s | body=%s",
                r.status_code,
                merged_ctx,
                body,
            )
            raise RuntimeError(
                f"OANDA request failed | status={r.status_code} | context={merged_ctx} | body={body}"
            )
        return r.json()


    def get_account_nav(self) -> float:
        """
        Fetch live account NAV (Net Asset Value) from OANDA.
        This is the true account value including unrealised P&L.
        Used for real-time position sizing.
        """
        data = self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/summary",
            context={
                "account_id": self.account_id,
                "order_type": "ACCOUNT_SUMMARY",
                "units": 0,
            },
        )
        account = data.get("account", {})
        nav = account.get("NAV") or account.get("balance")
        if nav is None:
            raise RuntimeError(
                f"Could not read NAV from account summary: {account}"
            )
        return float(nav)

    def get_candles(self, instrument: str, granularity: str, count: int = 100) -> dict[str, Any]:
        params = {
            "granularity": granularity,
            "count": count,
            "price": "M",
        }
        return self._request(
            "GET",
            f"/v3/instruments/{instrument}/candles",
            params=params,
            context={
                "account_id": self.account_id,
                "instrument": instrument,
                "granularity": granularity,
                "count": count,
                "order_type": "NONE",
                "units": 0,
            },
        )

    def place_market_order(
        self,
        *,
        units: int,
        instrument: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> dict[str, Any]:
        """
        Places a MARKET order, optionally with attached SL/TP ("bracket"/OCO-style).
        OANDA supports attached orders via stopLossOnFill / takeProfitOnFill.

        Notes:
          - prices must be strings
          - timeInForce for attached orders typically "GTC"
        """
        order: dict[str, Any] = {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(int(units)),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "clientExtensions": self._order_extensions(),
            "tradeClientExtensions": self._order_extensions(),
        }

        # Attach SL/TP if provided
        if take_profit is not None:
            order["takeProfitOnFill"] = {
                "price": self._fmt_price(instrument, float(take_profit)),
                "timeInForce": "GTC",
            }

        if stop_loss is not None:
            order["stopLossOnFill"] = {
                "price": self._fmt_price(instrument, float(stop_loss)),
                "timeInForce": "GTC",
            }

        payload = {"order": order}
        return self._request(
            "POST",
            f"/v3/accounts/{self.account_id}/orders",
            payload=payload,
            context={
                "account_id": self.account_id,
                "instrument": instrument,
                "order_type": "MARKET",
                "units": int(units),
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "tag": self.order_tag,
            },
        )

    def place_limit_order(
        self,
        *,
        units: int,
        instrument: str,
        price: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        time_in_force: str = "GTC",
    ) -> dict[str, Any]:
        """
        Places a LIMIT order at a specified price, optionally with attached SL/TP.
        """
        order: dict[str, Any] = {
            "type": "LIMIT",
            "instrument": instrument,
            "units": str(int(units)),
            "price": self._fmt_price(instrument, float(price)),
            "timeInForce": time_in_force,
            "positionFill": "DEFAULT",
            "clientExtensions": self._order_extensions(),
            "tradeClientExtensions": self._order_extensions(),
        }

        if take_profit is not None:
            order["takeProfitOnFill"] = {
                "price": self._fmt_price(instrument, float(take_profit)),
                "timeInForce": "GTC",
            }

        if stop_loss is not None:
            order["stopLossOnFill"] = {
                "price": self._fmt_price(instrument, float(stop_loss)),
                "timeInForce": "GTC",
            }

        payload = {"order": order}
        return self._request(
            "POST",
            f"/v3/accounts/{self.account_id}/orders",
            payload=payload,
            context={
                "account_id": self.account_id,
                "instrument": instrument,
                "order_type": "LIMIT",
                "units": int(units),
                "price": price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "tag": self.order_tag,
            },
        )

    def place_stop_order(
        self,
        *,
        units: int,
        instrument: str,
        price: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        time_in_force: str = "GTC",
    ) -> dict[str, Any]:
        order: dict[str, Any] = {
            "type": "STOP",
            "instrument": instrument,
            "units": str(int(units)),
            "price": self._fmt_price(instrument, float(price)),
            "timeInForce": time_in_force,
            "positionFill": "DEFAULT",
            "clientExtensions": self._order_extensions(),
            "tradeClientExtensions": self._order_extensions(),
        }
        if take_profit is not None:
            order["takeProfitOnFill"] = {"price": self._fmt_price(instrument, float(take_profit)), "timeInForce": "GTC"}
        if stop_loss is not None:
            order["stopLossOnFill"] = {"price": self._fmt_price(instrument, float(stop_loss)), "timeInForce": "GTC"}
        payload = {"order": order}
        return self._request(
            "POST",
            f"/v3/accounts/{self.account_id}/orders",
            payload=payload,
            context={
                "account_id": self.account_id,
                "instrument": instrument,
                "order_type": "STOP",
                "units": int(units),
                "price": price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "tag": self.order_tag,
            },
        )

    def list_pending_orders(self) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/pendingOrders",
            context={"account_id": self.account_id, "order_type": "LIST_PENDING", "units": 0},
        )

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"/v3/accounts/{self.account_id}/orders/{order_id}/cancel",
            context={"account_id": self.account_id, "order_type": "CANCEL", "order_id": order_id, "units": 0},
        )

    def list_open_trades(self) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/openTrades",
            context={"account_id": self.account_id, "order_type": "LIST_OPEN_TRADES", "units": 0},
        )

    def close_trade(self, trade_id: str, units: str = "ALL") -> dict[str, Any]:
        payload = {"units": units}
        return self._request(
            "PUT",
            f"/v3/accounts/{self.account_id}/trades/{trade_id}/close",
            payload=payload,
            context={"account_id": self.account_id, "order_type": "CLOSE_TRADE", "trade_id": trade_id, "units": units},
        )

    def get_trade(self, trade_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/trades/{trade_id}",
            context={"account_id": self.account_id, "order_type": "GET_TRADE", "trade_id": trade_id, "units": 0},
        )

    def get_price(self, instrument: str) -> dict[str, float] | None:
        """Returns current bid/ask for an instrument."""
        resp = self._request(
            "GET",
            f"/v3/accounts/{self.account_id}/pricing",
            params={"instruments": instrument},
            context={"account_id": self.account_id, "order_type": "GET_PRICE", "units": 0},
        )
        prices = resp.get("prices", [])
        if not prices:
            return None
        return {
            "bid": float(prices[0]["bids"][0]["price"]),
            "ask": float(prices[0]["asks"][0]["price"]),
        }

    def modify_trade(self, trade_id: str, stop_loss: float) -> dict[str, Any]:
        """Modify an open trade's stop loss."""
        payload = {
            "stopLoss": {
                "price": str(round(stop_loss, 5)),
                "timeInForce": "GTC",
            }
        }
        return self._request(
            "PUT",
            f"/v3/accounts/{self.account_id}/trades/{trade_id}/orders",
            payload=payload,
            context={"account_id": self.account_id, "order_type": "MODIFY_TRADE", "units": 0},
        )

    def close_trade_partial(self, trade_id: str, units: int) -> dict[str, Any]:
        """Close a partial number of units on an open trade."""
        return self.close_trade(trade_id, units=str(abs(units)))
