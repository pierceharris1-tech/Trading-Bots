"""
list_chats.py
Run once to find the channel IDs you want to watch.
Usage: python3 list_chats.py
"""

import asyncio
import os
from pathlib import Path
from telethon import TelegramClient

def load_env(path="/etc/qtp/oanda.env"):
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

load_env()

API_ID   = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]

async def main():
    async with TelegramClient("/etc/qtp/telegram_copier", API_ID, API_HASH) as client:
        print(f"\n{'ID':<20} {'Type':<12} {'Title'}")
        print("-" * 60)
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            kind = type(entity).__name__.replace("Channel", "Channel/Group")
            print(f"{dialog.id:<20} {kind:<12} {dialog.name}")

asyncio.run(main())
