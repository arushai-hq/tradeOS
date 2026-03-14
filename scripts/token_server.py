#!/usr/bin/env python3
"""
TradeOS — Token Callback Server

Lightweight HTTP server that captures Zerodha OAuth request_token,
exchanges it for an access_token, and writes to secrets.yaml.

Binds to 0.0.0.0:7291 (port not exposed externally — Docker bridge needs non-localhost bind).
Auto-shuts down after successful capture or 2-hour safety timeout.

Flow:
  1. Nginx proxies https://srv1332119.hstgr.cloud:11443/callback → localhost:7291
  2. Extract request_token from query params
  3. kite.generate_session(request_token, api_secret) → access_token
  4. Write to secrets.yaml + verify via kite.profile()
  5. Telegram confirmation + shutdown

NOTE: For automated daily flow, use token_cron.py to start this server.
      Can also be started manually: python scripts/token_server.py
"""

import os
import signal
import sys
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import pytz
import yaml
from kiteconnect import KiteConnect

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent
SECRETS_FILE = ROOT / "config" / "secrets.yaml"
SIGNAL_FILE = Path("/tmp/tradeos_token_ready")
PID_FILE = Path("/tmp/tradeos_token_server.pid")
IST = pytz.timezone("Asia/Kolkata")

# Safety timeout: auto-shutdown after 2 hours
AUTO_SHUTDOWN_SECONDS = 2 * 60 * 60

# Server reference for shutdown
_server: HTTPServer | None = None

# ---------------------------------------------------------------------------
# HTML templates
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{
  background: #1a1a2e;
  color: #ffffff;
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 100vh;
  margin: 0;
  padding: 20px;
  box-sizing: border-box;
}}
.card {{
  text-align: center;
  max-width: 500px;
}}
h1 {{ font-size: 2em; margin-bottom: 0.5em; }}
p {{ font-size: 1.2em; color: #aaa; }}
.status {{ font-size: 3em; }}
</style>
</head>
<body>
<div class="card">
  <div class="status">{icon}</div>
  <h1>{title}</h1>
  <p>{message}</p>
  <p style="color: #555; font-size: 0.8em;">TradeOS — Arushai Systems</p>
</div>
</body>
</html>"""


def _html(icon: str, title: str, message: str) -> str:
    return _HTML_TEMPLATE.format(icon=icon, title=title, message=message)


# ---------------------------------------------------------------------------
# Helpers (reused from refresh_token.py patterns)
# ---------------------------------------------------------------------------

def _load_secrets() -> dict:
    with open(SECRETS_FILE) as f:
        return yaml.safe_load(f)


def _save_secrets(secrets: dict) -> None:
    with open(SECRETS_FILE, "w") as f:
        yaml.dump(secrets, f, default_flow_style=False, allow_unicode=True)


def _ist_today() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


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


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class CallbackHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path != "/callback":
            self._respond(404, _html(
                "🚫", "Not Found", "Only /callback is served.",
            ))
            return

        params = parse_qs(parsed.query)
        request_token = params.get("request_token", [None])[0]

        if not request_token:
            self._respond(400, _html(
                "⚠️", "Missing Token",
                "Missing request_token. Please retry the login flow.",
            ))
            return

        try:
            self._handle_callback(request_token)
        except Exception as exc:
            secrets = _load_secrets()
            error_msg = str(exc)
            _send_telegram(
                secrets,
                f"❌ Token exchange failed: {error_msg}",
            )
            self._respond(500, _html(
                "❌", "Token Exchange Failed",
                f"Request token may be expired. Please re-login.<br>"
                f"<small style='color:#666'>{error_msg}</small>",
            ))

    def _handle_callback(self, request_token: str) -> None:
        secrets = _load_secrets()
        z = secrets.get("zerodha", {})
        api_key = z.get("api_key", "")
        api_secret = z.get("api_secret", "")

        if not api_key or not api_secret:
            self._respond(500, _html(
                "❌", "Configuration Error",
                "api_key or api_secret missing in secrets.yaml.",
            ))
            return

        # Exchange request_token for access_token
        kite = KiteConnect(api_key=api_key)
        data = kite.generate_session(request_token, api_secret=api_secret)
        access_token = data["access_token"]
        today = _ist_today()

        # Write to secrets.yaml
        secrets["zerodha"]["access_token"] = access_token
        secrets["zerodha"]["token_date"] = today
        _save_secrets(secrets)

        # Verify
        kite.set_access_token(access_token)
        try:
            profile = kite.profile()
            user_name = profile.get("user_name", "unknown")
        except Exception as exc:
            _send_telegram(
                secrets,
                f"❌ Token verification failed: {exc}",
            )
            self._respond(500, _html(
                "❌", "Verification Failed",
                f"Token saved but verification failed: {exc}",
            ))
            return

        # Success — notify + signal + respond + shutdown
        _send_telegram(
            secrets,
            f"✅ Token refreshed. User: {user_name}. TradeOS ready to boot.",
        )

        # Write signal file
        SIGNAL_FILE.touch()

        self._respond(200, _html(
            "✅", "TradeOS Authenticated",
            "You can close this tab.",
        ))

        # Schedule shutdown (2s delay so response is sent)
        threading.Timer(2.0, _shutdown_server).start()

    def _respond(self, code: int, body: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format, *args):
        """Suppress default stderr logging."""
        pass


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

def _shutdown_server() -> None:
    global _server
    if _server:
        _server.shutdown()


def _cleanup() -> None:
    """Remove PID file on exit."""
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def main() -> None:
    global _server

    # Clean slate
    SIGNAL_FILE.unlink(missing_ok=True)

    # Write PID file
    PID_FILE.write_text(str(os.getpid()))

    # Safety timeout — auto-shutdown after 2 hours
    shutdown_timer = threading.Timer(AUTO_SHUTDOWN_SECONDS, _shutdown_server)
    shutdown_timer.daemon = True
    shutdown_timer.start()

    # Handle SIGTERM gracefully
    def _handle_sigterm(signum, frame):
        _shutdown_server()
    signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        _server = HTTPServer(("0.0.0.0", 7291), CallbackHandler)
        print(f"TradeOS token server listening on 0.0.0.0:7291")
        print(f"Waiting for Zerodha callback... (auto-shutdown in 2h)")
        _server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Last-resort Telegram on crash
        try:
            secrets = _load_secrets()
            _send_telegram(secrets, f"❌ Token server crashed: {exc}")
        except Exception:
            pass
    finally:
        shutdown_timer.cancel()
        _cleanup()
        print("Token server stopped.")


if __name__ == "__main__":
    main()
