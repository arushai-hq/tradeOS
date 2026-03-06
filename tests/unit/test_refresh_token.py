"""
Unit tests for scripts/refresh_token.py helper functions.
"""
from __future__ import annotations

import re
from io import StringIO
from unittest.mock import MagicMock, mock_open, patch

import pytest

# scripts/ is not a package — import via importlib
import importlib.util
import sys
from pathlib import Path

_script_path = Path(__file__).parent.parent.parent / "scripts" / "refresh_token.py"
spec = importlib.util.spec_from_file_location("refresh_token", _script_path)
refresh_token = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh_token)


# ---------------------------------------------------------------------------
# test_ist_today_returns_correct_format
# ---------------------------------------------------------------------------

def test_ist_today_returns_correct_format():
    """Result matches YYYY-MM-DD pattern."""
    result = refresh_token.ist_today()
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", result), (
        f"Expected YYYY-MM-DD, got: {result!r}"
    )


# ---------------------------------------------------------------------------
# test_load_secrets_returns_dict
# ---------------------------------------------------------------------------

def test_load_secrets_returns_dict():
    """Mock open with valid yaml → returns dict with zerodha key."""
    yaml_content = (
        "zerodha:\n"
        "  api_key: testkey\n"
        "  api_secret: testsecret\n"
        "  access_token: testtoken\n"
        "  token_date: 2026-03-06\n"
    )
    with patch("builtins.open", mock_open(read_data=yaml_content)):
        result = refresh_token.load_secrets()

    assert isinstance(result, dict)
    assert "zerodha" in result
    assert result["zerodha"]["api_key"] == "testkey"


# ---------------------------------------------------------------------------
# test_save_secrets_writes_correctly
# ---------------------------------------------------------------------------

def test_save_secrets_writes_correctly():
    """Mock open → verify yaml.dump called with correct data."""
    secrets = {
        "zerodha": {
            "api_key": "testkey",
            "access_token": "newtoken",
            "token_date": "2026-03-07",
        }
    }
    m = mock_open()
    with patch("builtins.open", m):
        with patch("yaml.dump") as mock_dump:
            refresh_token.save_secrets(secrets)
            mock_dump.assert_called_once_with(
                secrets,
                m.return_value.__enter__.return_value,
                default_flow_style=False,
                allow_unicode=True,
            )


# ---------------------------------------------------------------------------
# test_send_telegram_skips_if_no_credentials
# ---------------------------------------------------------------------------

def test_send_telegram_skips_if_no_credentials():
    """Empty bot_token → no requests.post call made."""
    secrets = {"telegram": {"bot_token": "", "chat_id": ""}}
    with patch("requests.post") as mock_post:
        refresh_token.send_telegram(secrets, "test message")
        mock_post.assert_not_called()


def test_send_telegram_skips_if_telegram_key_missing():
    """No telegram key in secrets → no requests.post call made."""
    secrets = {}
    with patch("requests.post") as mock_post:
        refresh_token.send_telegram(secrets, "test message")
        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# test_send_telegram_skips_on_exception
# ---------------------------------------------------------------------------

def test_send_telegram_skips_on_exception():
    """requests.post raises → no exception propagates."""
    secrets = {"telegram": {"bot_token": "tok", "chat_id": "123"}}
    with patch("requests.post", side_effect=RuntimeError("network error")):
        # Must not raise
        refresh_token.send_telegram(secrets, "test message")
