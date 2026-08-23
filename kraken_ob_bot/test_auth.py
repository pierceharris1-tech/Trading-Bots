"""
One-off dry-run check before trusting kraken_client.py with real orders.
Run this on the EC2 box after `source ~/.kraken_env`:

    python3 test_auth.py

Does NOT place any real order -- the AddOrder call uses validate=true,
which Kraken checks (auth, params, permissions, pair, sizing) without
ever sending it to the matching engine.
"""
import os
from kraken_client import KrakenClient, KrakenAPIError

def main():
    api_key = os.environ.get("KRAKEN_API_KEY", "")
    api_secret = os.environ.get("KRAKEN_API_SECRET", "")
    if not api_key or not api_secret:
        print("KRAKEN_API_KEY / KRAKEN_API_SECRET not set -- did you `source ~/.kraken_env`?")
        return

    client = KrakenClient(api_key, api_secret, pair="XBTUSD")

    print("1. Resolving pair via public AssetPairs (no auth)...")
    try:
        resolved = client.resolve_pair()
        print(f"   OK -- 'XBTUSD' resolves to Kraken pair key: {resolved}")
    except KrakenAPIError as e:
        print(f"   FAILED: {e}")
        return

    print("\n2. Checking auth via private Balance call...")
    try:
        balance = client.get_balance()
        usd = balance.get("ZUSD", "0")
        print(f"   OK -- auth works. ZUSD balance: {usd}")
    except KrakenAPIError as e:
        print(f"   FAILED: {e}")
        print("   Check: API key/secret correct? Permissions include 'Query Funds'?")
        return

    print("\n3. Checking TradeBalance (margin info)...")
    try:
        tb = client.get_trade_balance()
        print(f"   OK -- equity={tb.get('e')}, free_margin={tb.get('mf')}, margin_level={tb.get('ml', 'n/a')}")
    except KrakenAPIError as e:
        print(f"   FAILED: {e}")
        return

    print("\n4. Validate-only AddOrder (checks signing + order construction, places NOTHING)...")
    try:
        ticker = client.get_ticker()
        last = ticker["last"]
        test_price = round(last * 0.5, 1)   # deliberately far from market so it could never fill
        test_sl = round(test_price * 0.99, 1)
        resp = client.add_order("buy", volume=0.0001, price=test_price, sl_price=test_sl,
                                 leverage=5, validate=True)
        print(f"   OK -- Kraken accepted the order construction (validate=true): {resp}")
    except KrakenAPIError as e:
        print(f"   FAILED: {e}")
        print("   Check: 'Create & modify orders' permission enabled? Margin trading enabled on account?")
        return

    print("\nAll checks passed. Auth, signing, and order construction are confirmed working.")

if __name__ == "__main__":
    main()
