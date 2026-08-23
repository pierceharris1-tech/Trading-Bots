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
# Matches the /home/ubuntu/logs/bots/ convention the other trading-bot@
# systemd services already use, rather than a bot-local logs/ folder.
LOG_DIR = "/home/ubuntu/logs/bots"
STATE_PATH = os.environ.get("OB_BOT_STATE_PATH", f"{LOG_DIR}/kraken-ob-bot-state.json")
TRADE_LOG_PATH = f"{LOG_DIR}/kraken-ob-bot-trades.log"
ERROR_LOG_PATH = f"{LOG_DIR}/kraken-ob-bot-errors.log"
