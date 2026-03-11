#!/usr/bin/env python3
"""
TradeOS — Daily Zerodha Token Refresh
Run every morning before starting TradeOS.
Usage: python scripts/refresh_token.py
Time: ~90 seconds
"""

import sys
import webbrowser
from pathlib import Path
from datetime import datetime
import pytz
import yaml
from kiteconnect import KiteConnect

# Paths
ROOT = Path(__file__).parent.parent
SECRETS_FILE = ROOT / "config" / "secrets.yaml"


def load_secrets() -> dict:
    with open(SECRETS_FILE) as f:
        return yaml.safe_load(f)


def save_secrets(secrets: dict) -> None:
    with open(SECRETS_FILE, "w") as f:
        yaml.dump(secrets, f, default_flow_style=False, allow_unicode=True)


def ist_today() -> str:
    ist = pytz.timezone("Asia/Kolkata")
    return datetime.now(ist).strftime("%Y-%m-%d")


def _resolve_trading_credentials(secrets: dict) -> tuple[str, str]:
    """Resolve trading channel credentials — supports new nested + old flat format."""
    tg = secrets.get("telegram", {})
    if not isinstance(tg, dict):
        return ("", "")
    # New format: telegram.trading.bot_token
    trading = tg.get("trading", {})
    if isinstance(trading, dict) and trading.get("bot_token"):
        return (str(trading.get("bot_token", "")), str(trading.get("chat_id", "")))
    # Old flat format: telegram.bot_token
    return (str(tg.get("bot_token", "")), str(tg.get("chat_id", "")))


def send_telegram(secrets: dict, message: str) -> None:
    """Non-blocking Telegram notification. Silently skips if not configured."""
    try:
        token, chat_id = _resolve_trading_credentials(secrets)
        if not token or not chat_id:
            return
        import requests
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=5
        )
    except Exception:
        pass  # Telegram is non-critical


def main():
    if not hasattr(sys, 'real_prefix') and \
       not (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix):
        print("⚠️  WARNING: Not running in a virtual environment.")
        print("   Run: source activate.sh")
        print("   Continuing anyway...")

    print("=" * 55)
    print("  TradeOS — Zerodha Token Refresh")
    print(f"  Date: {ist_today()} IST")
    print("=" * 55)

    # Load secrets
    secrets = load_secrets()
    z = secrets.get("zerodha", {})
    api_key = z.get("api_key", "")
    api_secret = z.get("api_secret", "")

    if not api_key or not api_secret:
        print("ERROR: api_key or api_secret missing in config/secrets.yaml")
        sys.exit(1)

    # Check if token already valid for today
    current_date = z.get("token_date", "")
    if current_date == ist_today():
        print(f"\nToken date is already today ({ist_today()}).")
        print("Verifying existing token...")
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(z.get("access_token", ""))
        try:
            profile = kite.profile()
            print(f"✅ Token already valid. User: {profile['user_name']}")
            print("No refresh needed. TradeOS is ready to boot.")
            sys.exit(0)
        except Exception:
            print("Token expired despite today's date. Refreshing...")

    # Generate login URL and open browser
    kite = KiteConnect(api_key=api_key)
    login_url = kite.login_url()

    print(f"\nStep 1: Opening Zerodha login in your browser...")
    print(f"        URL: {login_url}")
    webbrowser.open(login_url)

    print("\nStep 2: Log in with your Zerodha credentials + 2FA")
    print("        After login, browser redirects to:")
    print("        https://127.0.0.1/?request_token=XXXXXXXX&...")
    print()

    # Get request_token from user
    request_token = input("Step 3: Paste the request_token from the URL: ").strip()
    if not request_token:
        print("ERROR: No request_token provided.")
        sys.exit(1)

    # Generate session
    print("\nGenerating access token...")
    try:
        data = kite.generate_session(request_token, api_secret=api_secret)
    except Exception as e:
        print(f"ERROR: Failed to generate session: {e}")
        print("Common cause: request_token is single-use. "
              "If you already tried it, re-run the script for a fresh URL.")
        sys.exit(1)

    new_token = data["access_token"]
    today = ist_today()

    # Update secrets.yaml
    secrets["zerodha"]["access_token"] = new_token
    secrets["zerodha"]["token_date"] = today
    save_secrets(secrets)
    print(f"✅ secrets.yaml updated: token_date={today}")

    # Verify
    kite.set_access_token(new_token)
    try:
        profile = kite.profile()
        print(f"✅ Token valid. User: {profile['user_name']} | "
              f"Broker: {profile['broker']}")
    except Exception as e:
        print(f"ERROR: Token verification failed: {e}")
        sys.exit(1)

    # Telegram notification
    msg = (f"✅ TradeOS token refreshed\n"
           f"User: {profile['user_name']}\n"
           f"Date: {today} IST\n"
           f"TradeOS ready to boot.")
    send_telegram(secrets, msg)

    print()
    print("=" * 55)
    print("  Token refresh complete. TradeOS ready.")
    print(f"  Next step: python main.py")
    print("=" * 55)


if __name__ == "__main__":
    main()
