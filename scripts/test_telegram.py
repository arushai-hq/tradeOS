#!/usr/bin/env python3
"""
TradeOS — Telegram connection test.

Sends a single test message to confirm Telegram is configured correctly.
Safe to run at any time — does not start any engine components.

Usage:
    python scripts/test_telegram.py
"""
import asyncio
from datetime import datetime
from pathlib import Path

import pytz
import yaml

ROOT = Path(__file__).parent.parent


def _load_secrets() -> dict:
    secrets_path = ROOT / "config" / "secrets.yaml"
    if not secrets_path.exists():
        raise FileNotFoundError(
            f"secrets.yaml not found at {secrets_path}\n"
            "Run: cp config/secrets.yaml.template config/secrets.yaml"
        )
    with open(secrets_path) as f:
        return yaml.safe_load(f)


async def _send_test() -> None:
    import sys
    sys.path.insert(0, str(ROOT))
    from utils.telegram import send_telegram

    secrets = _load_secrets()

    from utils.telegram import resolve_telegram_credentials
    bot_token, chat_id = resolve_telegram_credentials(secrets, "trading")

    if not bot_token or not chat_id:
        print("❌ Missing telegram credentials in secrets.yaml (check telegram.trading.bot_token/chat_id)")
        return

    user_id = secrets.get("zerodha", {}).get("user_id", "unknown")
    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist).strftime("%H:%M:%S IST")

    msg = (
        "🔔 TradeOS Telegram test\n"
        "✅ Connection working\n"
        f"User: {user_id}\n"
        f"Time: {now_ist}\n"
        "Environment: VPS"
    )

    shared_state = {"telegram_active": True}
    await send_telegram(msg, shared_state, secrets)

    if shared_state.get("telegram_active"):
        print("✅ Telegram working — message sent")
    else:
        print("❌ Send failed — check bot_token and chat_id in secrets.yaml")


if __name__ == "__main__":
    asyncio.run(_send_test())
