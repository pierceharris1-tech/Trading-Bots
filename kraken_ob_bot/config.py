"""
All tunables in one place. API keys come from environment variables --
never hardcode them here or commit them.
"""
import os

# --- Kraken API ---
KRAKEN_API_KEY = os.environ.get("KRAKEN_API_KEY", "")
KRAKEN_API_SECRET = os.environ.get("KRAKEN_API_SECRET", "")
PAIR = "XBTUSD"

# --- Strategy (locked, GA-optimised + OOS-validated -- see strategy.WINNER) ---
CANDLE_MINUTES = 3
BUFFER_BARS = 1000  # rolling candle window kept in memory / passed to find_order_blocks

# --- Risk ---
RISK_PCT = 0.02
LEVERAGE = 5
POS_A_PCT = 0.90
POS_B_PCT = 0.10
MARGIN_FLOOR_PCT = 200

# --- Loop timing ---
LOOP_SECONDS = 180  # 3 minutes, aligned to candle close

# --- Paths ---
STATE_PATH = os.environ.get("OB_BOT_STATE_PATH", "logs/state.json")
TRADE_LOG_PATH = "logs/trades.log"
ERROR_LOG_PATH = "logs/errors.log"
