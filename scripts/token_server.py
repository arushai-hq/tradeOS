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

import logging
import os
import signal
import subprocess
import sys
import threading
import time
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SECRETS_FILE = ROOT / "config" / "secrets.yaml"
SETTINGS_FILE = ROOT / "config" / "settings.yaml"
SIGNAL_FILE = Path("/tmp/tradeos_token_ready")
PID_FILE = Path("/tmp/tradeos_token_server.pid")
IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# File + console logging (stdlib)
# ---------------------------------------------------------------------------

def _configure_token_logging() -> logging.Logger:
    """Configure basic file + console logging for token server."""
    log_dir = ROOT / "logs" / "token"
    log_dir.mkdir(parents=True, exist_ok=True)

    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    log_path = log_dir / f"token_{today_str}.log"

    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    datefmt = "%Y-%m-%dT%H:%M:%S"

    file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))

    logger = logging.getLogger("tradeos.token")
    logger.handlers.clear()
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    return logger


_token_log = _configure_token_logging()

# ---------------------------------------------------------------------------
# Config defaults (used if token_automation section missing from settings.yaml)
# ---------------------------------------------------------------------------

_DEFAULTS = {
    "server": {"port": 7291, "timeout_hours": 2},
    "auto_start": {
        "enabled": True,
        "weekdays_only": True,
        "tradeos_dir": "/opt/tradeOS",
        "tmux_session_name": "tradeos",
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


# Load config at module level
_config = _load_token_config()
_server_cfg = _config.get("server", _DEFAULTS["server"])
_auto_cfg = _config.get("auto_start", _DEFAULTS["auto_start"])

SERVER_PORT = _server_cfg.get("port", 7291)
AUTO_SHUTDOWN_SECONDS = _server_cfg.get("timeout_hours", 2) * 60 * 60
TRADEOS_DIR = _auto_cfg.get("tradeos_dir", "/opt/tradeOS")
VENV_PYTHON = f"{TRADEOS_DIR}/.venv/bin/python"
TMUX_SESSION = _auto_cfg.get("tmux_session_name", "tradeos")
AUTO_START_ENABLED = _auto_cfg.get("enabled", True)
AUTO_START_WEEKDAYS_ONLY = _auto_cfg.get("weekdays_only", True)

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
# Auto-start main.py (weekdays only)
# ---------------------------------------------------------------------------

def _auto_start_main(secrets: dict, user_name: str) -> None:
    """Start main.py in a named tmux session after successful token refresh.

    Skips on weekends (if weekdays_only). Kills stale tmux session if present.
    Never raises — auto-start failure must not crash the token server.
    """
    if not AUTO_START_ENABLED:
        _token_log.info("Auto-start disabled in config")
        _send_telegram(
            secrets,
            f"✅ Token refreshed. User: {user_name}. TradeOS ready to boot.",
        )
        return

    now = datetime.now(IST)
    if AUTO_START_WEEKDAYS_ONLY and now.weekday() >= 5:  # Saturday=5, Sunday=6
        _token_log.info("Weekend — skipping main.py auto-start")
        _send_telegram(
            secrets,
            f"✅ Token refreshed. User: {user_name}. Weekend — main.py not started.",
        )
        return

    try:
        # Kill stale tmux session if exists
        result = subprocess.run(
            ["tmux", "has-session", "-t", TMUX_SESSION],
            capture_output=True,
        )
        if result.returncode == 0:
            subprocess.run(["tmux", "kill-session", "-t", TMUX_SESSION])
            _token_log.info("Killed stale tmux session")

        # Start main.py in new tmux session
        subprocess.Popen([
            "tmux", "new-session", "-d", "-s", TMUX_SESSION,
            "-c", TRADEOS_DIR,
            VENV_PYTHON, "main.py",
        ])

        # Verify after 5 seconds
        time.sleep(5)
        result = subprocess.run(
            ["tmux", "has-session", "-t", TMUX_SESSION],
            capture_output=True,
        )
        if result.returncode == 0:
            _token_log.info(f"main.py started in tmux session '{TMUX_SESSION}'")
            _send_telegram(
                secrets,
                f"✅ Token refreshed. User: {user_name}. "
                f"main.py started (tmux: {TMUX_SESSION}).\n"
                f"📄 Log: logs/tradeos/tradeos_{_ist_today()}.log",
            )
        else:
            _token_log.warning("tmux session not found after start")
            _send_telegram(
                secrets,
                f"✅ Token refreshed. User: {user_name}. "
                f"⚠️ main.py failed to auto-start.",
            )
    except Exception as exc:
        _token_log.error(f"Auto-start failed: {exc}")
        _send_telegram(
            secrets,
            f"✅ Token refreshed. User: {user_name}. "
            f"⚠️ main.py failed to auto-start.",
        )


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
            _token_log.error(f"Token exchange failed: {exc}", exc_info=True)
            try:
                secrets = _load_secrets()
                _send_telegram(
                    secrets,
                    f"❌ Token exchange failed: {exc}",
                )
            except Exception:
                pass
            self._respond(500, _html(
                "❌", "Authentication Failed",
                "Authentication failed. Please retry.",
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
            _token_log.error(f"Token verification failed: {exc}", exc_info=True)
            _send_telegram(
                secrets,
                f"❌ Token verification failed: {exc}",
            )
            self._respond(500, _html(
                "❌", "Verification Failed",
                "Token saved but verification failed. Check server logs.",
            ))
            return

        # Success — auto-start main.py (sends Telegram with status)
        _auto_start_main(secrets, user_name)

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
        _server = HTTPServer(("0.0.0.0", SERVER_PORT), CallbackHandler)
        _token_log.info(f"Token server listening on 0.0.0.0:{SERVER_PORT}")
        _token_log.info("Waiting for Zerodha callback (auto-shutdown in 2h)")
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
        _token_log.info("Token server stopped")


if __name__ == "__main__":
    main()
