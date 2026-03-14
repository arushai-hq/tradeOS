#!/usr/bin/env python3
"""
TradeOS — Token Cron Orchestrator

Daily orchestrator for automated Zerodha token refresh.
Called by crontab at 07:00 IST (01:30 UTC) on weekdays.

Flow:
  1. Check if token already valid for today → skip
  2. Kill stale token_server if running
  3. Start token_server.py as background subprocess
  4. Send Zerodha login URL to Telegram
  5. Escalation loop: check every 30s for signal file
     - 07:30 → reminder
     - 08:00 → warning
     - 08:30 → final warning
     - 08:45 → give up, kill server

Usage: python scripts/token_cron.py
"""

import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytz
import yaml
from kiteconnect import KiteConnect

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent
SECRETS_FILE = ROOT / "config" / "secrets.yaml"
TOKEN_SERVER_SCRIPT = ROOT / "scripts" / "token_server.py"
SIGNAL_FILE = Path("/tmp/tradeos_token_ready")
PID_FILE = Path("/tmp/tradeos_token_server.pid")
IST = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_secrets() -> dict:
    with open(SECRETS_FILE) as f:
        return yaml.safe_load(f)


def _ist_now() -> datetime:
    return datetime.now(IST)


def _ist_today() -> str:
    return _ist_now().strftime("%Y-%m-%d")


def _resolve_trading_credentials(secrets: dict) -> tuple[str, str]:
    """Resolve trading channel credentials — supports nested + flat format."""
    tg = secrets.get("telegram", {})
    if not isinstance(tg, dict):
        return ("", "")
    trading = tg.get("trading", {})
    if isinstance(trading, dict) and trading.get("bot_token"):
        return (str(trading.get("bot_token", "")), str(trading.get("chat_id", "")))
    return (str(tg.get("bot_token", "")), str(tg.get("chat_id", "")))


def _send_telegram(secrets: dict, message: str) -> None:
    """Synchronous Telegram send. Silently skips if not configured."""
    try:
        token, chat_id = _resolve_trading_credentials(secrets)
        if not token or not chat_id:
            return
        import requests
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=5,
        )
    except Exception:
        pass


def _kill_stale_server() -> None:
    """Kill stale token_server process if PID file exists."""
    if not PID_FILE.exists():
        return
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        print(f"Killed stale token_server (PID {pid})")
    except (ProcessLookupError, ValueError):
        pass
    PID_FILE.unlink(missing_ok=True)


def _is_token_captured() -> bool:
    return SIGNAL_FILE.exists()


# ---------------------------------------------------------------------------
# Escalation schedule
# ---------------------------------------------------------------------------

# (hour, minute) thresholds in IST
_REMINDER_0730 = (7, 30)
_WARNING_0800 = (8, 0)
_FINAL_0830 = (8, 30)
_EXPIRED_0845 = (8, 45)


def _past_threshold(now: datetime, threshold: tuple[int, int]) -> bool:
    h, m = threshold
    return (now.hour, now.minute) >= (h, m)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    secrets = _load_secrets()
    z = secrets.get("zerodha", {})
    api_key = z.get("api_key", "")

    if not api_key:
        print("ERROR: api_key missing in config/secrets.yaml")
        sys.exit(1)

    # Check if token already valid for today
    token_date = z.get("token_date", "")
    if token_date == _ist_today():
        msg = f"✅ Token already valid for today ({_ist_today()}). Skipping."
        print(msg)
        _send_telegram(secrets, msg)
        return

    # Kill stale server
    _kill_stale_server()

    # Clean stale signal file
    SIGNAL_FILE.unlink(missing_ok=True)

    # Start token_server.py
    print("Starting token_server.py...")
    subprocess.Popen(
        [sys.executable, str(TOKEN_SERVER_SCRIPT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2)

    # Generate login URL
    kite = KiteConnect(api_key=api_key)
    login_url = kite.login_url()

    # Send initial Telegram message
    _send_telegram(secrets, (
        "🔑 TradeOS Daily Authentication\n"
        f"Tap to login → {login_url}\n"
        "After login + TOTP, token is captured automatically.\n"
        "⏰ Window: 07:00 - 08:45 IST"
    ))
    print(f"Login URL sent to Telegram: {login_url}")

    # Escalation loop
    sent_0730 = False
    sent_0800 = False
    sent_0830 = False

    while True:
        time.sleep(30)

        if _is_token_captured():
            _send_telegram(secrets, "✅ Token captured. Authentication complete.")
            print("Token captured. Done.")
            return

        now = _ist_now()

        # 08:45 — expired
        if _past_threshold(now, _EXPIRED_0845):
            _send_telegram(
                secrets,
                "❌ Token refresh window expired. No trading today.",
            )
            _kill_stale_server()
            print("Token window expired (08:45 IST). Exiting.")
            return

        # 08:30 — final warning
        if not sent_0830 and _past_threshold(now, _FINAL_0830):
            _send_telegram(secrets, (
                "🚨 FINAL WARNING: Token not refreshed!\n"
                "Market opens in 45 minutes.\n"
                f"Tap NOW → {login_url}"
            ))
            sent_0830 = True

        # 08:00 — warning
        elif not sent_0800 and _past_threshold(now, _WARNING_0800):
            _send_telegram(secrets, (
                "⚠️ TradeOS: 1 hour to market open.\n"
                "Token still not refreshed!\n"
                f"Tap to login → {login_url}"
            ))
            sent_0800 = True

        # 07:30 — reminder
        elif not sent_0730 and _past_threshold(now, _REMINDER_0730):
            _send_telegram(secrets, (
                "⏰ Reminder: Token not yet refreshed.\n"
                f"Tap to login → {login_url}"
            ))
            sent_0730 = True


if __name__ == "__main__":
    main()
