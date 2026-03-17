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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SECRETS_FILE = ROOT / "config" / "secrets.yaml"
SETTINGS_FILE = ROOT / "config" / "settings.yaml"
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
# Config defaults (used if token_automation section missing)
# ---------------------------------------------------------------------------

_DEFAULTS = {
    "cron": {
        "reminders": ["07:30", "08:00"],
        "final_warning": "08:30",
        "deadline": "08:45",
        "check_interval_seconds": 30,
    },
}


def _load_token_config() -> dict:
    """Load token_automation config from settings.yaml with defaults fallback."""
    try:
        with open(SETTINGS_FILE) as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("token_automation", _DEFAULTS)
    except Exception:
        return _DEFAULTS


def _parse_time(time_str: str) -> tuple[int, int]:
    """Parse 'HH:MM' string to (hour, minute) tuple."""
    parts = time_str.split(":")
    return (int(parts[0]), int(parts[1]))


# Load config at module level
_config = _load_token_config()
_cron_cfg = _config.get("cron", _DEFAULTS["cron"])

_REMINDERS = [_parse_time(t) for t in _cron_cfg.get("reminders", ["07:30", "08:00"])]
_FINAL_WARNING = _parse_time(_cron_cfg.get("final_warning", "08:30"))
_DEADLINE = _parse_time(_cron_cfg.get("deadline", "08:45"))
CHECK_INTERVAL = _cron_cfg.get("check_interval_seconds", 30)


# ---------------------------------------------------------------------------
# Escalation schedule
# ---------------------------------------------------------------------------

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

    from utils.progress import spinner, step_done, step_fail, step_info

    # Kill stale server
    _kill_stale_server()

    # Clean stale signal file
    SIGNAL_FILE.unlink(missing_ok=True)

    # Start token_server.py
    with spinner("Starting token server..."):
        subprocess.Popen(
            [sys.executable, str(TOKEN_SERVER_SCRIPT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(2)
    step_done("Token server started")

    # Generate login URL
    kite = KiteConnect(api_key=api_key)
    login_url = kite.login_url()

    # Send initial Telegram message
    with spinner("Sending login URL to Telegram..."):
        _send_telegram(secrets, (
            "🔑 TradeOS Daily Authentication\n"
            f"Tap to login → {login_url}\n"
            "After login + TOTP, token is captured automatically.\n"
            "⏰ Window: 07:00 - 08:45 IST | Auto-starts main.py after auth"
        ))
    step_done(f"Login URL sent to Telegram")

    # Escalation loop — track which reminders have been sent
    sent_reminders = [False] * len(_REMINDERS)
    sent_final = False

    while True:
        time.sleep(CHECK_INTERVAL)

        if _is_token_captured():
            _send_telegram(secrets, "✅ Token captured. Authentication complete.")
            step_done("Token captured — authentication complete")
            return

        now = _ist_now()

        # Deadline — expired
        if _past_threshold(now, _DEADLINE):
            _send_telegram(
                secrets,
                "❌ Token refresh window expired. No trading today.",
            )
            _kill_stale_server()
            step_fail(f"Token window expired ({_DEADLINE[0]:02d}:{_DEADLINE[1]:02d} IST)")
            return

        # Final warning
        if not sent_final and _past_threshold(now, _FINAL_WARNING):
            _send_telegram(secrets, (
                "🚨 FINAL WARNING: Token not refreshed!\n"
                "Market opens in 45 minutes.\n"
                f"Tap NOW → {login_url}"
            ))
            sent_final = True

        # Reminders (in reverse order so latest fires first via elif)
        else:
            for i in range(len(_REMINDERS) - 1, -1, -1):
                if not sent_reminders[i] and _past_threshold(now, _REMINDERS[i]):
                    _send_telegram(secrets, (
                        "⏰ Reminder: Token not yet refreshed.\n"
                        f"Tap to login → {login_url}"
                    ))
                    sent_reminders[i] = True
                    break


if __name__ == "__main__":
    main()
