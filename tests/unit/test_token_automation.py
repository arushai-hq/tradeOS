"""
Tests for token automation: token_server.py and token_cron.py.

Covers:
  (a) /callback with valid request_token → 200, secrets updated, signal file created
  (b) /callback with missing request_token → 400
  (c) /callback with failed generate_session → 500
  (d) token_cron skips when token_date is today
  (e) token_cron escalation timing logic
"""

import io
import os
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock, call
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import HTTPError

import pytest
import pytz
import yaml

IST = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_SECRETS = {
    "zerodha": {
        "api_key": "test_api_key",
        "api_secret": "test_api_secret",
        "access_token": "old_token",
        "token_date": "2026-01-01",
    },
    "telegram": {
        "trading": {
            "bot_token": "fake_bot_token",
            "chat_id": "fake_chat_id",
        },
    },
}


def _write_secrets(path: Path, data: dict) -> None:
    with open(path, "w") as f:
        yaml.dump(data, f)


def _read_secrets(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _start_test_server(handler_class, port: int) -> HTTPServer:
    """Start a test server on a random port and return it."""
    server = HTTPServer(("127.0.0.1", port), handler_class)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ---------------------------------------------------------------------------
# (a) /callback with valid request_token → 200
# ---------------------------------------------------------------------------

class TestTokenServerSuccess:
    """Test successful callback flow."""

    def test_callback_valid_token(self, tmp_path):
        """Valid request_token → access_token exchanged, secrets updated, signal file."""
        secrets_file = tmp_path / "secrets.yaml"
        signal_file = tmp_path / "signal_ready"
        pid_file = tmp_path / "pid"
        _write_secrets(secrets_file, _FAKE_SECRETS)

        # Patch token_server module-level constants and KiteConnect
        with (
            patch("scripts.token_server.SECRETS_FILE", secrets_file),
            patch("scripts.token_server.SIGNAL_FILE", signal_file),
            patch("scripts.token_server.PID_FILE", pid_file),
            patch("scripts.token_server._send_telegram") as mock_tg,
            patch("scripts.token_server.KiteConnect") as MockKite,
            patch("scripts.token_server._shutdown_server"),
        ):
            # Configure mock KiteConnect
            mock_kite = MagicMock()
            MockKite.return_value = mock_kite
            mock_kite.generate_session.return_value = {
                "access_token": "new_access_token_123",
            }
            mock_kite.profile.return_value = {
                "user_name": "TestUser",
                "user_id": "AB1234",
            }

            from scripts.token_server import CallbackHandler

            # Start server on a free port
            server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
            port = server.server_address[1]
            thread = threading.Thread(target=server.handle_request, daemon=True)
            thread.start()

            # Make request
            url = f"http://127.0.0.1:{port}/callback?request_token=test_req_token"
            response = urlopen(url, timeout=5)

            assert response.status == 200
            body = response.read().decode()
            assert "TradeOS Authenticated" in body

            # Verify secrets updated
            updated = _read_secrets(secrets_file)
            assert updated["zerodha"]["access_token"] == "new_access_token_123"

            # Verify signal file created
            assert signal_file.exists()

            # Verify Telegram sent
            mock_tg.assert_called()
            call_args = mock_tg.call_args[0]
            assert "Token refreshed" in call_args[1]

            server.server_close()


# ---------------------------------------------------------------------------
# (b) /callback with missing request_token → 400
# ---------------------------------------------------------------------------

class TestTokenServerMissingToken:
    """Test callback without request_token."""

    def test_callback_missing_token(self, tmp_path):
        """Missing request_token → 400."""
        secrets_file = tmp_path / "secrets.yaml"
        _write_secrets(secrets_file, _FAKE_SECRETS)

        with (
            patch("scripts.token_server.SECRETS_FILE", secrets_file),
            patch("scripts.token_server.SIGNAL_FILE", tmp_path / "signal"),
            patch("scripts.token_server.PID_FILE", tmp_path / "pid"),
        ):
            from scripts.token_server import CallbackHandler

            server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
            port = server.server_address[1]
            thread = threading.Thread(target=server.handle_request, daemon=True)
            thread.start()

            url = f"http://127.0.0.1:{port}/callback"
            with pytest.raises(HTTPError) as exc_info:
                urlopen(url, timeout=5)

            assert exc_info.value.code == 400

            server.server_close()


# ---------------------------------------------------------------------------
# (c) /callback with failed generate_session → 500
# ---------------------------------------------------------------------------

class TestTokenServerFailedSession:
    """Test callback when generate_session fails."""

    def test_callback_session_failure(self, tmp_path):
        """generate_session raises → 500 + Telegram error."""
        secrets_file = tmp_path / "secrets.yaml"
        _write_secrets(secrets_file, _FAKE_SECRETS)

        with (
            patch("scripts.token_server.SECRETS_FILE", secrets_file),
            patch("scripts.token_server.SIGNAL_FILE", tmp_path / "signal"),
            patch("scripts.token_server.PID_FILE", tmp_path / "pid"),
            patch("scripts.token_server._send_telegram") as mock_tg,
            patch("scripts.token_server.KiteConnect") as MockKite,
        ):
            mock_kite = MagicMock()
            MockKite.return_value = mock_kite
            mock_kite.generate_session.side_effect = Exception("Token expired")

            from scripts.token_server import CallbackHandler

            server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
            port = server.server_address[1]
            thread = threading.Thread(target=server.handle_request, daemon=True)
            thread.start()

            url = f"http://127.0.0.1:{port}/callback?request_token=expired_token"
            with pytest.raises(HTTPError) as exc_info:
                urlopen(url, timeout=5)

            assert exc_info.value.code == 500

            # Verify error Telegram sent
            mock_tg.assert_called()
            error_call = mock_tg.call_args[0][1]
            assert "Token exchange failed" in error_call

            server.server_close()


# ---------------------------------------------------------------------------
# (d) token_cron skips when token_date is today
# ---------------------------------------------------------------------------

class TestTokenCronSkip:
    """Test that token_cron skips if token is already valid today."""

    def test_skip_when_token_valid_today(self, tmp_path):
        """token_date == today → send skip message and return."""
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        secrets = _FAKE_SECRETS.copy()
        secrets = {**_FAKE_SECRETS}
        secrets["zerodha"] = {**_FAKE_SECRETS["zerodha"], "token_date": today_str}

        secrets_file = tmp_path / "secrets.yaml"
        _write_secrets(secrets_file, secrets)

        with (
            patch("scripts.token_cron.SECRETS_FILE", secrets_file),
            patch("scripts.token_cron._send_telegram") as mock_tg,
        ):
            from scripts.token_cron import main
            main()

            mock_tg.assert_called_once()
            msg = mock_tg.call_args[0][1]
            assert "already valid" in msg


# ---------------------------------------------------------------------------
# (e) token_cron escalation timing logic
# ---------------------------------------------------------------------------

class TestTokenCronEscalation:
    """Test escalation messages are sent at correct thresholds."""

    def test_escalation_thresholds(self):
        """Verify _past_threshold logic for each escalation time."""
        from scripts.token_cron import _past_threshold

        # 07:29 → not past 07:30
        t_0729 = datetime(2026, 3, 16, 7, 29, tzinfo=IST)
        assert not _past_threshold(t_0729, (7, 30))

        # 07:30 → past 07:30
        t_0730 = datetime(2026, 3, 16, 7, 30, tzinfo=IST)
        assert _past_threshold(t_0730, (7, 30))

        # 07:59 → not past 08:00
        t_0759 = datetime(2026, 3, 16, 7, 59, tzinfo=IST)
        assert not _past_threshold(t_0759, (8, 0))

        # 08:00 → past 08:00
        t_0800 = datetime(2026, 3, 16, 8, 0, tzinfo=IST)
        assert _past_threshold(t_0800, (8, 0))

        # 08:29 → not past 08:30
        t_0829 = datetime(2026, 3, 16, 8, 29, tzinfo=IST)
        assert not _past_threshold(t_0829, (8, 30))

        # 08:30 → past 08:30
        t_0830 = datetime(2026, 3, 16, 8, 30, tzinfo=IST)
        assert _past_threshold(t_0830, (8, 30))

        # 08:45 → past 08:45 (expired)
        t_0845 = datetime(2026, 3, 16, 8, 45, tzinfo=IST)
        assert _past_threshold(t_0845, (8, 45))

    def test_escalation_sends_correct_messages(self, tmp_path):
        """Simulate escalation loop with time advancing — verify messages."""
        secrets_file = tmp_path / "secrets.yaml"
        signal_file = tmp_path / "signal"
        pid_file = tmp_path / "pid"
        _write_secrets(secrets_file, _FAKE_SECRETS)

        call_count = 0
        time_sequence = [
            # Each call to _ist_now() returns the next time
            datetime(2026, 3, 16, 7, 0, tzinfo=IST),   # initial check
            datetime(2026, 3, 16, 7, 15, tzinfo=IST),  # loop 1 — no threshold
            datetime(2026, 3, 16, 7, 30, tzinfo=IST),  # loop 2 — 07:30 reminder
            datetime(2026, 3, 16, 8, 0, tzinfo=IST),   # loop 3 — 08:00 warning
            datetime(2026, 3, 16, 8, 30, tzinfo=IST),  # loop 4 — 08:30 final
            datetime(2026, 3, 16, 8, 45, tzinfo=IST),  # loop 5 — expired
        ]

        def mock_ist_now():
            nonlocal call_count
            idx = min(call_count, len(time_sequence) - 1)
            call_count += 1
            return time_sequence[idx]

        telegram_messages = []
        def mock_send_tg(secrets, msg):
            telegram_messages.append(msg)

        with (
            patch("scripts.token_cron.SECRETS_FILE", secrets_file),
            patch("scripts.token_cron.SIGNAL_FILE", signal_file),
            patch("scripts.token_cron.PID_FILE", pid_file),
            patch("scripts.token_cron.TOKEN_SERVER_SCRIPT", tmp_path / "fake_server.py"),
            patch("scripts.token_cron._ist_now", side_effect=mock_ist_now),
            patch("scripts.token_cron._ist_today", return_value="2026-03-16"),
            patch("scripts.token_cron._send_telegram", side_effect=mock_send_tg),
            patch("scripts.token_cron._kill_stale_server"),
            patch("scripts.token_cron.time.sleep"),
            patch("scripts.token_cron.subprocess.Popen"),
            patch("scripts.token_cron.KiteConnect") as MockKite,
        ):
            mock_kite = MagicMock()
            MockKite.return_value = mock_kite
            mock_kite.login_url.return_value = "https://kite.zerodha.com/connect/login?v=3&api_key=test"

            from scripts.token_cron import main
            main()

        # Should have: initial + reminder + warning + final + expired = 5 messages
        assert len(telegram_messages) == 5

        # Verify message content
        assert "Daily Authentication" in telegram_messages[0]
        assert "Reminder" in telegram_messages[1]
        assert "Reminder" in telegram_messages[2]
        assert "FINAL WARNING" in telegram_messages[3]
        assert "expired" in telegram_messages[4]


# ---------------------------------------------------------------------------
# (f) Auto-start main.py on weekday
# ---------------------------------------------------------------------------

class TestAutoStartWeekday:
    """Test auto-start fires on weekdays."""

    def test_auto_start_weekday(self):
        """Monday → tmux new-session called with correct args."""
        # Monday 2026-03-16
        monday = datetime(2026, 3, 16, 7, 30, tzinfo=IST)

        mock_run = MagicMock()
        # has-session returns 1 (no existing session)
        mock_run.return_value = MagicMock(returncode=1)
        mock_popen = MagicMock()

        with (
            patch("scripts.token_server.datetime") as mock_dt,
            patch("scripts.token_server.subprocess.run", mock_run),
            patch("scripts.token_server.subprocess.Popen", mock_popen),
            patch("scripts.token_server.time.sleep"),
            patch("scripts.token_server._send_telegram") as mock_tg,
        ):
            mock_dt.now.return_value = monday
            # After Popen, has-session returns 0 (session exists)
            mock_run.side_effect = [
                MagicMock(returncode=1),  # first has-session check
                MagicMock(returncode=0),  # verify after start
            ]

            from scripts.token_server import _auto_start_main
            _auto_start_main({"telegram": {}}, "TestUser")

        # Verify tmux new-session called
        popen_args = mock_popen.call_args[0][0]
        assert "tmux" in popen_args
        assert "new-session" in popen_args
        assert "tradeos" in popen_args
        assert "main.py" in popen_args

        # Verify success Telegram
        tg_msg = mock_tg.call_args[0][1]
        assert "main.py started" in tg_msg


# ---------------------------------------------------------------------------
# (g) Auto-start skipped on weekend
# ---------------------------------------------------------------------------

class TestAutoStartWeekend:
    """Test auto-start is skipped on weekends."""

    def test_auto_start_weekend(self):
        """Saturday → subprocess NOT called, Telegram says Weekend."""
        saturday = datetime(2026, 3, 14, 7, 30, tzinfo=IST)

        mock_popen = MagicMock()

        with (
            patch("scripts.token_server.datetime") as mock_dt,
            patch("scripts.token_server.subprocess.Popen", mock_popen),
            patch("scripts.token_server._send_telegram") as mock_tg,
        ):
            mock_dt.now.return_value = saturday

            from scripts.token_server import _auto_start_main
            _auto_start_main({"telegram": {}}, "TestUser")

        # subprocess.Popen should NOT be called
        mock_popen.assert_not_called()

        # Telegram should mention "Weekend"
        tg_msg = mock_tg.call_args[0][1]
        assert "Weekend" in tg_msg


# ---------------------------------------------------------------------------
# (h) Auto-start kills stale tmux session
# ---------------------------------------------------------------------------

class TestAutoStartStaleSession:
    """Test stale tmux session is killed before starting new one."""

    def test_auto_start_stale_session(self):
        """Existing tmux session → kill-session called before new-session."""
        monday = datetime(2026, 3, 16, 7, 30, tzinfo=IST)

        run_calls = []
        def mock_run(cmd, **kwargs):
            run_calls.append(cmd)
            # has-session returns 0 (session exists) for both calls
            return MagicMock(returncode=0)

        with (
            patch("scripts.token_server.datetime") as mock_dt,
            patch("scripts.token_server.subprocess.run", side_effect=mock_run),
            patch("scripts.token_server.subprocess.Popen"),
            patch("scripts.token_server.time.sleep"),
            patch("scripts.token_server._send_telegram"),
        ):
            mock_dt.now.return_value = monday

            from scripts.token_server import _auto_start_main
            _auto_start_main({"telegram": {}}, "TestUser")

        # Should have: has-session, kill-session, has-session (verify)
        assert any("kill-session" in cmd for cmd in run_calls)
        # kill-session should come before the verify has-session
        kill_idx = next(i for i, c in enumerate(run_calls) if "kill-session" in c)
        assert kill_idx == 1  # after first has-session


# ---------------------------------------------------------------------------
# (i) Auto-start failure doesn't crash
# ---------------------------------------------------------------------------

class TestAutoStartFailure:
    """Test auto-start failure sends Telegram but doesn't crash."""

    def test_auto_start_failure(self):
        """subprocess.Popen raises → Telegram failure message, no crash."""
        monday = datetime(2026, 3, 16, 7, 30, tzinfo=IST)

        with (
            patch("scripts.token_server.datetime") as mock_dt,
            patch("scripts.token_server.subprocess.run", MagicMock(returncode=1)),
            patch("scripts.token_server.subprocess.Popen", side_effect=OSError("tmux not found")),
            patch("scripts.token_server._send_telegram") as mock_tg,
        ):
            mock_dt.now.return_value = monday

            from scripts.token_server import _auto_start_main
            # Should not raise
            _auto_start_main({"telegram": {}}, "TestUser")

        # Verify failure Telegram
        tg_msg = mock_tg.call_args[0][1]
        assert "failed to auto-start" in tg_msg


# ---------------------------------------------------------------------------
# (j) Config missing fallback — defaults used
# ---------------------------------------------------------------------------

class TestConfigMissingFallback:
    """Test that both scripts work with missing token_automation config."""

    def test_server_defaults_when_config_missing(self, tmp_path):
        """Missing settings.yaml → server uses default port/timeout."""
        fake_settings = tmp_path / "settings.yaml"
        # Write settings without token_automation section
        with open(fake_settings, "w") as f:
            yaml.dump({"system": {"mode": "paper"}}, f)

        with patch("scripts.token_server.SETTINGS_FILE", fake_settings):
            from scripts.token_server import _load_token_config, _DEFAULTS

            config = _load_token_config()
            server_cfg = config.get("server", _DEFAULTS["server"])
            auto_cfg = config.get("auto_start", _DEFAULTS["auto_start"])

            assert server_cfg.get("port", 7291) == 7291
            assert server_cfg.get("timeout_hours", 2) == 2
            assert auto_cfg.get("enabled", True) is True
            assert auto_cfg.get("weekdays_only", True) is True

    def test_cron_defaults_when_config_missing(self, tmp_path):
        """Missing settings.yaml → cron uses default timing."""
        fake_settings = tmp_path / "settings.yaml"
        # Write settings without token_automation section
        with open(fake_settings, "w") as f:
            yaml.dump({"system": {"mode": "paper"}}, f)

        with patch("scripts.token_cron.SETTINGS_FILE", fake_settings):
            from scripts.token_cron import _load_token_config, _parse_time, _DEFAULTS

            config = _load_token_config()
            cron_cfg = config.get("cron", _DEFAULTS["cron"])

            reminders = [_parse_time(t) for t in cron_cfg.get("reminders", ["07:30", "08:00"])]
            assert reminders == [(7, 30), (8, 0)]

            final = _parse_time(cron_cfg.get("final_warning", "08:30"))
            assert final == (8, 30)

            deadline = _parse_time(cron_cfg.get("deadline", "08:45"))
            assert deadline == (8, 45)

            assert cron_cfg.get("check_interval_seconds", 30) == 30
