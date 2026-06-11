"""
signal_parser.py
Uses Claude API to parse raw Telegram messages into structured trade signals.
Handles complete entries, split entries, management commands, and noise.
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional
import anthropic

logger = logging.getLogger(__name__)

client = anthropic.Anthropic()

# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class EntrySignal:
    instrument_raw: str
    direction: str               # "buy" or "sell"
    order_type: str              # "market" or "limit"
    entry_price: Optional[float]
    sl: Optional[float]
    tp1: Optional[float]
    tp2: Optional[float]
    tp3: Optional[float]         # None = open (no TP, hold until told)
    high_risk: bool = False
    raw_text: str = ""

    @property
    def is_complete(self) -> bool:
        return self.sl is not None

@dataclass
class SLTPUpdate:
    instrument_raw: Optional[str]
    sl: Optional[float]
    tp1: Optional[float]
    tp2: Optional[float]
    tp3: Optional[float]
    raw_text: str = ""

@dataclass
class ManagementSignal:
    action: str                  # "sl_to_be" | "close_all" | "close_instrument"
                                 # "cancel_pending" | "cancel_all_pending" | "move_order"
    instrument_raw: Optional[str]
    entry_price_ref: Optional[float]
    new_price: Optional[float]
    reply_to_msg_id: Optional[int]
    raw_text: str = ""

@dataclass
class ParseResult:
    signal_type: str             # "entry" | "sltp_update" | "management" | "noise"
    entry: Optional[EntrySignal] = None
    sltp_update: Optional[SLTPUpdate] = None
    management: Optional[ManagementSignal] = None

# ── Prompt ─────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a parser for Telegram forex/indices trading signal messages.
Extract structured trade data and return ONLY valid JSON, no markdown, no explanation.

Return one of these four structures:

1. Complete or partial entry signal:
{
  "type": "entry",
  "instrument": "<e.g. GOLD, GBPJPY, US30, GER30, SPX500>",
  "direction": "buy" | "sell",
  "order_type": "market" | "limit",
  "entry_price": <number or null for market>,
  "sl": <number or null if not given>,
  "tp1": <number or null>,
  "tp2": <number or null>,
  "tp3": <number or null>,
  "high_risk": true | false
}

2. SL/TP update only (followup to a previous entry, no new entry price):
{
  "type": "sltp_update",
  "instrument": "<instrument or null if not mentioned>",
  "sl": <number or null>,
  "tp1": <number or null>,
  "tp2": <number or null>,
  "tp3": <number or null>
}

3. Management signal:
{
  "type": "management",
  "action": "sl_to_be" | "close_all" | "close_instrument" | "cancel_pending" | "cancel_all_pending" | "move_order",
  "instrument": "<instrument or null>",
  "entry_price_ref": <number or null>,
  "new_price": <number or null>
}

4. Noise:
{
  "type": "noise"
}

Rules:
- "Buy limit" / "Sell limit" -> order_type = "limit", "Buy" / "Sell" alone -> "market"
- "Tp: Open" or "Tp3: Open" -> that tp = null (means hold open)
- "High risk" prefix -> high_risk = true
- If message has ONLY SL/TP values with no direction/entry -> type = "sltp_update"
- "SL at BE", "move SL to breakeven", "risk free", "sl to be" -> action = "sl_to_be"
- "close all", "I've closed", "out", "closed fully", "I'm done" -> action = "close_all"
- "close [instrument]" specifically -> action = "close_instrument"
- "delete", "cancel", "missed so delete" referring to a specific order price -> action = "cancel_pending", entry_price_ref = that price
- "deleting every limit", "delete all", "cancel all pending" -> action = "cancel_all_pending"
- "move [order] to [price]", "changing entry to" -> action = "move_order", entry_price_ref = old price, new_price = new price
- "close partials", "close 50%", "close 75%", "take profits", "running X pips" -> noise
- Celebrations, general chat -> noise
- "Apply proper risk" is a standard footer, ignore it
"""

# ── Parser ─────────────────────────────────────────────────────────────────────

def parse_message(text: str, reply_to_msg_id: Optional[int] = None) -> ParseResult:
    if _is_obvious_noise(text):
        return ParseResult(signal_type="noise")

    model = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

    try:
        response = client.messages.create(
            model=model,
            max_tokens=500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text}]
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"```json|```", "", raw).strip()
        data = json.loads(raw)

    except json.JSONDecodeError as e:
        logger.warning(f"JSON parse error: {e}")
        return ParseResult(signal_type="noise")
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return ParseResult(signal_type="noise")

    sig_type = data.get("type", "noise")

    if sig_type == "entry":
        try:
            entry = EntrySignal(
                instrument_raw=data["instrument"],
                direction=data["direction"],
                order_type=data.get("order_type", "market"),
                entry_price=float(data["entry_price"]) if data.get("entry_price") else None,
                sl=float(data["sl"]) if data.get("sl") else None,
                tp1=float(data["tp1"]) if data.get("tp1") else None,
                tp2=float(data["tp2"]) if data.get("tp2") else None,
                tp3=float(data["tp3"]) if data.get("tp3") else None,
                high_risk=data.get("high_risk", False),
                raw_text=text
            )
            return ParseResult(signal_type="entry", entry=entry)
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"Entry parse error: {e} | {data}")
            return ParseResult(signal_type="noise")

    elif sig_type == "sltp_update":
        update = SLTPUpdate(
            instrument_raw=data.get("instrument"),
            sl=float(data["sl"]) if data.get("sl") else None,
            tp1=float(data["tp1"]) if data.get("tp1") else None,
            tp2=float(data["tp2"]) if data.get("tp2") else None,
            tp3=float(data["tp3"]) if data.get("tp3") else None,
            raw_text=text
        )
        return ParseResult(signal_type="sltp_update", sltp_update=update)

    elif sig_type == "management":
        mgmt = ManagementSignal(
            action=data.get("action", "close_all"),
            instrument_raw=data.get("instrument"),
            entry_price_ref=float(data["entry_price_ref"]) if data.get("entry_price_ref") else None,
            new_price=float(data["new_price"]) if data.get("new_price") else None,
            reply_to_msg_id=reply_to_msg_id,
            raw_text=text
        )
        return ParseResult(signal_type="management", management=mgmt)

    return ParseResult(signal_type="noise")


def _is_obvious_noise(text: str) -> bool:
    if not text or len(text.strip()) < 4:
        return True
    noise_patterns = [
        r"^\d+pips",
        r"running \d+pips",
        r"let the week begin",
        r"have a wonderful",
        r"stay safe",
        r"good morning",
        r"good night",
        r"wishing everyone",
        r"great weekend",
    ]
    text_lower = text.lower().strip()
    for pat in noise_patterns:
        if re.search(pat, text_lower):
            if not re.search(r"\b(buy|sell|sl:|tp:|stop|cancel|delet|move)\b", text_lower):
                return True
    return False
