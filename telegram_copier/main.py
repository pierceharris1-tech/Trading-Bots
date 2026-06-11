"""
main.py
Telegram signal copier — main entry point.
Listens to configured channels via Telethon, parses signals with Claude,
executes trades on OANDA.

Pending queue: incomplete entry signals (no SL yet) are held per instrument.
When SL/TP followup arrives it completes the most recent pending entry and executes.
Pending signals expire after PENDING_TIMEOUT_SECS.
"""

import asyncio
import logging
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

from telethon import TelegramClient, events
from telethon.tl.types import Message

sys.path.insert(0, "/home/ubuntu/trading-bots")
from shared.oanda_client import OandaClient

from signal_parser import parse_message, EntrySignal
from trade_executor import execute_entry, execute_management, cancel_pending_order, cancel_all_pending_orders, move_pending_order
from instrument_map import resolve_instrument
import position_manager as pm

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("/home/ubuntu/logs/bots/telegram_copier.log"),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("telegram_copier")

# ── Config ─────────────────────────────────────────────────────────────────────

def _load_env(path="/etc/qtp/oanda.env"):
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_env()

TELEGRAM_API_ID   = int(os.environ["TELEGRAM_API_ID"])
TELEGRAM_API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION_PATH      = os.environ.get("TELEGRAM_SESSION", "/etc/qtp/telegram_copier")

WATCHED_CHANNELS_RAW = os.environ.get("TELEGRAM_CHANNELS", "")
WATCHED_CHANNELS = [int(c.strip()) for c in WATCHED_CHANNELS_RAW.split(",") if c.strip()]

PENDING_TIMEOUT_SECS = 300  # 5 minutes

if not WATCHED_CHANNELS:
    logger.error("No TELEGRAM_CHANNELS configured.")
    sys.exit(1)

# ── OANDA client ───────────────────────────────────────────────────────────────

oanda = OandaClient(
    base_url=os.environ.get("OANDA_BASE_URL", "https://api-fxtrade.oanda.com"),
    api_key=os.environ["OANDA_API_KEY"],
    account_id=os.environ["OANDA_ACCOUNT_ID"],
    bot_id="TELEGRAM_COPIER"
)

# ── Telegram client ────────────────────────────────────────────────────────────

telegram = TelegramClient(SESSION_PATH, TELEGRAM_API_ID, TELEGRAM_API_HASH)

# ── Pending queue ──────────────────────────────────────────────────────────────
# Structure: { instrument_raw: [ {entry: EntrySignal, msg_id: int, timestamp: float}, ... ] }
pending_signals: dict[str, list[dict]] = defaultdict(list)


def _expire_pending():
    """Remove pending signals older than PENDING_TIMEOUT_SECS."""
    now = time.time()
    for instrument in list(pending_signals.keys()):
        before = len(pending_signals[instrument])
        pending_signals[instrument] = [
            p for p in pending_signals[instrument]
            if now - p["timestamp"] < PENDING_TIMEOUT_SECS
        ]
        expired = before - len(pending_signals[instrument])
        if expired:
            logger.info(f"Expired {expired} pending signal(s) for {instrument}")


def _complete_pending(instrument_raw: str, sl: float, tp1, tp2, tp3) -> Optional[dict]:
    """
    Find the most recent incomplete pending signal for instrument_raw,
    fill in its SL/TP, and return it. Returns None if no pending found.
    """
    _expire_pending()
    queue = pending_signals.get(instrument_raw.upper(), [])
    # Find most recent (last in list) that has no SL yet
    for i in range(len(queue) - 1, -1, -1):
        if queue[i]["entry"].sl is None:
            queue[i]["entry"].sl = sl
            if tp1 is not None:
                queue[i]["entry"].tp1 = tp1
            if tp2 is not None:
                queue[i]["entry"].tp2 = tp2
            if tp3 is not None:
                queue[i]["entry"].tp3 = tp3
            return queue.pop(i)
    return None


# ── Message handler ────────────────────────────────────────────────────────────

@telegram.on(events.NewMessage(chats=WATCHED_CHANNELS))
async def handle_message(event: events.NewMessage.Event):
    message: Message = event.message
    text = message.text or ""

    if not text.strip():
        return

    reply_to_id = message.reply_to_msg_id if message.is_reply else None
    msg_id = message.id

    logger.info(f"[msg {msg_id}] {text[:120].replace(chr(10), ' ')}")

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, parse_message, text, reply_to_id
    )

    if result.signal_type == "noise":
        return

    # ── Entry signal ───────────────────────────────────────────────────────────
    if result.signal_type == "entry":
        sig = result.entry
        instrument_key = sig.instrument_raw.upper()

        if sig.is_complete:
            # Deduplication — skip if we already have open trades for this
            # instrument at the same entry price (e.g. Wizard sent correction)
            instrument = resolve_instrument(sig.instrument_raw)
            if instrument:
                existing = pm.get_open_trades_for_instrument(instrument)
                duplicate = any(
                    abs(t["entry_price"] - (sig.entry_price or 0)) < 0.01
                    for t in existing
                )
                if duplicate:
                    logger.info(f"[msg {msg_id}] DUPLICATE signal for "
                               f"{sig.instrument_raw} @ {sig.entry_price} — skipping")
                    return

            # Has SL — execute immediately
            hr = " [HIGH RISK]" if sig.high_risk else ""
            logger.info(f"[msg {msg_id}] ENTRY{hr}: {sig.direction.upper()} "
                        f"{sig.instrument_raw} Entry={sig.entry_price} "
                        f"SL={sig.sl} TP1={sig.tp1} TP2={sig.tp2} TP3={sig.tp3}")
            await loop.run_in_executor(None, execute_entry, oanda, sig, msg_id)
        else:
            # No SL yet — add to pending queue
            pending_signals[instrument_key].append({
                "entry": sig,
                "msg_id": msg_id,
                "timestamp": time.time()
            })
            logger.info(f"[msg {msg_id}] PENDING (no SL yet): "
                        f"{sig.direction.upper()} {sig.instrument_raw} "
                        f"@ {sig.entry_price} — awaiting SL/TP")

    # ── SL/TP update ───────────────────────────────────────────────────────────
    elif result.signal_type == "sltp_update":
        upd = result.sltp_update

        # Determine which instrument to look up
        instrument_raw = upd.instrument_raw
        if not instrument_raw:
            # No instrument mentioned — try all pending, pick most recent overall
            candidates = []
            for instr, queue in pending_signals.items():
                for item in queue:
                    if item["entry"].sl is None:
                        candidates.append((item["timestamp"], instr, item))
            if candidates:
                candidates.sort(reverse=True)
                instrument_raw = candidates[0][1]
            else:
                logger.info(f"[msg {msg_id}] SL/TP update but no pending signals found")
                return

        completed = _complete_pending(
            instrument_raw, upd.sl, upd.tp1, upd.tp2, upd.tp3
        )

        if completed:
            sig = completed["entry"]
            orig_msg_id = completed["msg_id"]
            logger.info(f"[msg {msg_id}] COMPLETED pending {sig.instrument_raw} "
                        f"from msg {orig_msg_id}: SL={sig.sl} TP1={sig.tp1} "
                        f"TP2={sig.tp2} TP3={sig.tp3} — executing")
            await loop.run_in_executor(None, execute_entry, oanda, sig, orig_msg_id)
        else:
            logger.info(f"[msg {msg_id}] SL/TP update for {instrument_raw} "
                        f"but no matching pending signal")

    # ── Management signal ──────────────────────────────────────────────────────
    elif result.signal_type == "management":
        mgmt = result.management
        action = mgmt.action

        logger.info(f"[msg {msg_id}] MANAGEMENT: {action} | "
                    f"instrument={mgmt.instrument_raw} | "
                    f"entry_ref={mgmt.entry_price_ref} | "
                    f"reply_to={reply_to_id}")

        if action == "cancel_all_pending":
            await loop.run_in_executor(None, cancel_all_pending_orders, oanda)

        elif action == "cancel_pending":
            instrument = resolve_instrument(mgmt.instrument_raw) if mgmt.instrument_raw else None
            await loop.run_in_executor(
                None, cancel_pending_order, oanda, instrument, mgmt.entry_price_ref
            )

        elif action == "move_order":
            instrument = resolve_instrument(mgmt.instrument_raw) if mgmt.instrument_raw else None
            await loop.run_in_executor(
                None, move_pending_order, oanda, instrument,
                mgmt.entry_price_ref, mgmt.new_price
            )

        else:
            # sl_to_be, close_all, close_instrument
            await loop.run_in_executor(
                None, execute_management, oanda, mgmt, msg_id
            )


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    pm.init_db()
    logger.info("Initialised positions DB")

    await telegram.start()
    logger.info("Telegram client started")

    me = await telegram.get_me()
    logger.info(f"Logged in as: {me.first_name} (@{me.username})")
    logger.info(f"Watching {len(WATCHED_CHANNELS)} channel(s): {WATCHED_CHANNELS}")
    logger.info("Telegram copier running — waiting for signals...")

    await telegram.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
