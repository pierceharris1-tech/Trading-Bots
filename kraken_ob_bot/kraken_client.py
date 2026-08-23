"""
Kraken REST API client — spot margin trading.
Direct HMAC-SHA512 auth, no third-party SDK.
"""
import time
import hmac
import hashlib
import base64
import urllib.parse
import requests

API_URL = "https://api.kraken.com"


class KrakenAPIError(Exception):
    pass


class KrakenClient:
    def __init__(self, api_key, api_secret, pair="XBTUSD"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.pair = pair
        self._altname_cache = None

    # ---------- auth ----------

    def _nonce(self):
        return str(int(time.time() * 1000))

    def _sign(self, urlpath, data):
        postdata = urllib.parse.urlencode(data)
        encoded = (str(data["nonce"]) + postdata).encode()
        message = urlpath.encode() + hashlib.sha256(encoded).digest()
        mac = hmac.new(base64.b64decode(self.api_secret), message, hashlib.sha512)
        return base64.b64encode(mac.digest()).decode()

    def _private(self, endpoint, data=None):
        data = dict(data or {})
        data["nonce"] = self._nonce()
        urlpath = f"/0/private/{endpoint}"
        headers = {
            "API-Key": self.api_key,
            "API-Sign": self._sign(urlpath, data),
        }
        resp = requests.post(API_URL + urlpath, headers=headers, data=data, timeout=15)
        return self._unwrap(resp)

    def _public(self, endpoint, params=None):
        resp = requests.get(f"{API_URL}/0/public/{endpoint}", params=params or {}, timeout=15)
        return self._unwrap(resp)

    def _unwrap(self, resp):
        resp.raise_for_status()
        body = resp.json()
        if body.get("error"):
            raise KrakenAPIError("; ".join(body["error"]))
        return body["result"]

    # ---------- pair resolution ----------

    def resolve_pair(self):
        """Confirm the tradable pair name via AssetPairs rather than assuming a format."""
        if self._altname_cache:
            return self._altname_cache
        pairs = self._public("AssetPairs")
        for key, info in pairs.items():
            if info.get("altname") == self.pair or key == self.pair:
                self._altname_cache = key
                return key
        raise KrakenAPIError(f"Pair {self.pair} not found in AssetPairs")

    # ---------- market data ----------

    def get_ohlc(self, interval=3, since=None):
        """
        interval: minutes. Kraken's public set is 1,5,15,30,60,240,1440,10080,21600 —
        3 is NOT a native interval, so 3M candles must be built by resampling 1-minute data.
        """
        params = {"pair": self.pair, "interval": interval}
        if since:
            params["since"] = since
        result = self._public("OHLC", params)
        key = [k for k in result if k != "last"][0]
        candles = result[key]
        return [
            {
                "time": int(c[0]),
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "vwap": float(c[5]),
                "volume": float(c[6]),
                "count": int(c[7]),
            }
            for c in candles
        ]

    def get_ticker(self):
        result = self._public("Ticker", {"pair": self.pair})
        key = list(result.keys())[0]
        return {"last": float(result[key]["c"][0])}

    # ---------- account ----------

    def get_balance(self):
        return self._private("Balance")

    def get_trade_balance(self, asset="ZUSD"):
        """Collateral balance, margin valuations, equity, margin level."""
        return self._private("TradeBalance", {"asset": asset})

    # ---------- orders ----------

    def add_order(self, side, volume, price, sl_price=None, ordertype="limit",
                  leverage=5, userref=None, validate=False, reduce_only=False):
        """
        side: 'buy' or 'sell'
        price: limit price (entry price, or close/TP price for a reduce_only order)
        sl_price: if given, attaches a stop-loss conditional close (only valid
                  on an ENTRY order — Kraken allows exactly one conditional
                  leg per AddOrder, confirmed against Kraken's docs). Leave
                  None for a plain closing order (e.g. the separate TP leg
                  position_manager places once an entry fills).
        reduce_only: set True for closing orders so they can never open a
                  new position in the opposite direction if size is ever
                  miscalculated.
        """
        data = {
            "pair": self.pair,
            "type": side,
            "ordertype": ordertype,
            "volume": f"{volume:.8f}",
            "price": f"{price:.2f}",
            "leverage": str(leverage),
        }
        if sl_price is not None:
            data["close[ordertype]"] = "stop-loss"
            data["close[price]"] = f"{sl_price:.2f}"
        if reduce_only:
            data["reduce_only"] = "true"
        if userref is not None:
            data["userref"] = str(userref)
        if validate:
            data["validate"] = "true"
        return self._private("AddOrder", data)

    def amend_order(self, txid, trigger_price=None, limit_price=None):
        """Move an existing order's stop trigger or limit price in-place (keeps queue position)."""
        data = {"txid": txid}
        if trigger_price is not None:
            data["trigger_price"] = f"{trigger_price:.2f}"
        if limit_price is not None:
            data["limit_price"] = f"{limit_price:.2f}"
        return self._private("AmendOrder", data)

    def cancel_order(self, txid):
        return self._private("CancelOrder", {"txid": txid})

    def get_open_orders(self):
        result = self._private("OpenOrders")
        return result.get("open", {})

    def get_closed_orders(self, start=None):
        data = {}
        if start:
            data["start"] = str(start)
        result = self._private("ClosedOrders", data)
        return result.get("closed", {})
