"""
TradeOS — Unit tests for multi-channel Telegram config.

Tests:
  (a) New nested format loads correctly for trading channel
  (b) New nested format loads correctly for hawk channel
  (c) Old flat format triggers backward compat for trading channel
  (d) Old flat format returns empty for non-trading channel
  (e) Empty channel config returns empty credentials (silent skip)
  (f) Missing telegram section returns empty credentials
  (g) send_telegram skips silently for unconfigured non-trading channel
  (h) send_telegram uses correct channel credentials
  (i) Deprecation warning logged once per session for flat format
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import structlog.testing

import utils.telegram as tg_module
from utils.telegram import resolve_telegram_credentials, send_telegram


# ---------------------------------------------------------------------------
# (a) New nested format — trading channel
# ---------------------------------------------------------------------------

def test_new_format_trading_channel():
    """New nested format returns correct trading credentials."""
    secrets = {
        "telegram": {
            "trading": {"bot_token": "tok_trading", "chat_id": "chat_trading"},
            "hawk": {"bot_token": "tok_hawk", "chat_id": "chat_hawk"},
        }
    }
    token, chat_id = resolve_telegram_credentials(secrets, "trading")
    assert token == "tok_trading"
    assert chat_id == "chat_trading"


# ---------------------------------------------------------------------------
# (b) New nested format — hawk channel
# ---------------------------------------------------------------------------

def test_new_format_hawk_channel():
    """New nested format returns correct hawk credentials."""
    secrets = {
        "telegram": {
            "trading": {"bot_token": "tok_trading", "chat_id": "chat_trading"},
            "hawk": {"bot_token": "tok_hawk", "chat_id": "chat_hawk"},
        }
    }
    token, chat_id = resolve_telegram_credentials(secrets, "hawk")
    assert token == "tok_hawk"
    assert chat_id == "chat_hawk"


# ---------------------------------------------------------------------------
# (c) Old flat format → backward compat for trading
# ---------------------------------------------------------------------------

def test_old_flat_format_backward_compat_trading():
    """Old flat format (telegram.bot_token) maps to trading channel."""
    # Reset the module-level warning flag
    tg_module._flat_format_warned = False
    secrets = {
        "telegram": {"bot_token": "old_tok", "chat_id": "old_chat"}
    }
    token, chat_id = resolve_telegram_credentials(secrets, "trading")
    assert token == "old_tok"
    assert chat_id == "old_chat"


# ---------------------------------------------------------------------------
# (d) Old flat format returns empty for non-trading channel
# ---------------------------------------------------------------------------

def test_old_flat_format_empty_for_hawk():
    """Old flat format can't resolve hawk — returns empty."""
    secrets = {
        "telegram": {"bot_token": "old_tok", "chat_id": "old_chat"}
    }
    token, chat_id = resolve_telegram_credentials(secrets, "hawk")
    assert token == ""
    assert chat_id == ""


# ---------------------------------------------------------------------------
# (e) Empty channel config — silent skip
# ---------------------------------------------------------------------------

def test_empty_channel_config_returns_empty():
    """Channel with empty bot_token returns empty credentials."""
    secrets = {
        "telegram": {
            "trading": {"bot_token": "tok_trading", "chat_id": "chat_trading"},
            "hawk": {"bot_token": "", "chat_id": ""},
        }
    }
    token, chat_id = resolve_telegram_credentials(secrets, "hawk")
    assert token == ""
    assert chat_id == ""


# ---------------------------------------------------------------------------
# (f) Missing telegram section
# ---------------------------------------------------------------------------

def test_missing_telegram_section():
    """No telegram key in secrets → empty credentials."""
    token, chat_id = resolve_telegram_credentials({}, "trading")
    assert token == ""
    assert chat_id == ""


def test_telegram_not_dict():
    """telegram key is not a dict → empty credentials."""
    token, chat_id = resolve_telegram_credentials({"telegram": "invalid"}, "trading")
    assert token == ""
    assert chat_id == ""


# ---------------------------------------------------------------------------
# (g) send_telegram skips silently for unconfigured non-trading channel
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_telegram_skips_unconfigured_hawk():
    """Sending to unconfigured hawk channel does not disable telegram_active."""
    secrets = {
        "telegram": {
            "trading": {"bot_token": "tok", "chat_id": "chat"},
            "hawk": {"bot_token": "", "chat_id": ""},
        }
    }
    shared_state = {"telegram_active": True}

    with patch("utils.telegram.asyncio.to_thread") as mock_thread:
        await send_telegram("test", shared_state, secrets, channel="hawk")
        mock_thread.assert_not_called()

    # telegram_active stays True — only trading channel failures disable it
    assert shared_state["telegram_active"] is True


# ---------------------------------------------------------------------------
# (h) send_telegram uses correct channel credentials
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_telegram_uses_channel_credentials():
    """send_telegram routes to the correct channel's bot_token/chat_id."""
    secrets = {
        "telegram": {
            "trading": {"bot_token": "tok_trading", "chat_id": "chat_trading"},
            "hawk": {"bot_token": "tok_hawk", "chat_id": "chat_hawk"},
        }
    }
    shared_state = {"telegram_active": True}

    with patch("utils.telegram.asyncio.to_thread") as mock_thread:
        await send_telegram("hello hawk", shared_state, secrets, channel="hawk")
        mock_thread.assert_called_once()
        # Verify the URL contains the hawk bot token
        call_args = mock_thread.call_args
        url = call_args[0][1]  # requests.post, url
        assert "tok_hawk" in url
        payload = call_args[1]["json"]
        assert payload["chat_id"] == "chat_hawk"


# ---------------------------------------------------------------------------
# (i) Deprecation warning logged once per session for flat format
# ---------------------------------------------------------------------------

def test_flat_format_deprecation_warning_logged_once():
    """Old flat format logs deprecation warning on first call only."""
    tg_module._flat_format_warned = False
    secrets = {"telegram": {"bot_token": "old", "chat_id": "old_chat"}}

    with structlog.testing.capture_logs() as cap_logs:
        resolve_telegram_credentials(secrets, "trading")
        resolve_telegram_credentials(secrets, "trading")

    deprecation_events = [
        e for e in cap_logs if e.get("event") == "telegram_config_deprecated"
    ]
    assert len(deprecation_events) == 1
    assert "Migrate to telegram.trading.bot_token" in deprecation_events[0]["note"]


# ---------------------------------------------------------------------------
# (j) Future channel extensibility
# ---------------------------------------------------------------------------

def test_arbitrary_future_channel():
    """Any future channel name resolves if configured."""
    secrets = {
        "telegram": {
            "trading": {"bot_token": "t1", "chat_id": "c1"},
            "hawk": {"bot_token": "t2", "chat_id": "c2"},
            "alerts": {"bot_token": "t3", "chat_id": "c3"},
        }
    }
    token, chat_id = resolve_telegram_credentials(secrets, "alerts")
    assert token == "t3"
    assert chat_id == "c3"
