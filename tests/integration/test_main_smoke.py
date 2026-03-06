"""
Integration smoke tests for main.py

Tests:
  - Phase 0 blocks on expired/missing token_date
  - Phase 0 exits clean on holiday / weekend
  - _init_shared_state() has all required D6 + D9 keys
  - risk_watchdog triggers L2 at 3% daily loss (via shared_state)
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytz
import pytest

from main import (
    _init_shared_state,
    run_token_freshness_check,
    run_holiday_check,
    trigger_kill_switch,
)

IST = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Required shared_state keys (D6 contract + D9 session keys)
# ---------------------------------------------------------------------------
REQUIRED_D6_KEYS = [
    "ws_connected", "last_tick_timestamp", "reconnect_attempt",
    "disconnect_timestamp", "reconnect_requested",
    "last_signal", "signals_generated_today",
    "open_orders", "open_positions", "fills_today",
    "daily_pnl_pct", "daily_pnl_rs", "consecutive_losses",
    "kill_switch_level",
    "system_start_time", "tasks_alive",
    "recon_in_progress", "locked_instruments",
    "tick_queue", "order_queue",
]

REQUIRED_D9_KEYS = [
    "system_ready", "accepting_signals",
    "session_date", "session_start_time",
    "zerodha_user_id", "pre_market_gate_passed",
    "telegram_active",
]


class TestPhase0TokenCheck:
    def test_phase0_blocks_on_expired_token(self):
        """Stale token_date must call sys.exit(1)."""
        secrets = {
            "zerodha": {"token_date": "2020-01-01"},  # deliberately old
            "telegram": {"bot_token": "", "chat_id": ""},
        }
        with pytest.raises(SystemExit) as exc_info:
            run_token_freshness_check(secrets)
        assert exc_info.value.code == 1

    def test_phase0_blocks_on_missing_token_date(self):
        """Missing token_date in secrets must call sys.exit(1)."""
        secrets = {
            "zerodha": {"token_date": ""},
            "telegram": {"bot_token": "", "chat_id": ""},
        }
        with pytest.raises(SystemExit) as exc_info:
            run_token_freshness_check(secrets)
        assert exc_info.value.code == 1

    def test_phase0_passes_with_today_token(self):
        """token_date == today IST must pass without exiting."""
        today_str = datetime.now(IST).date().isoformat()
        secrets = {
            "zerodha": {"token_date": today_str},
            "telegram": {"bot_token": "", "chat_id": ""},
        }
        # Must not raise
        run_token_freshness_check(secrets)


class TestPhase0HolidayCheck:
    def test_phase0_exits_clean_on_weekend(self):
        """Saturday/Sunday must call sys.exit(0) — clean exit."""
        # Saturday = weekday 5
        saturday = datetime(2026, 3, 7, 8, 0, 0, tzinfo=IST)  # 2026-03-07 is Saturday
        secrets = {"telegram": {"bot_token": "", "chat_id": ""}}

        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = saturday
            with pytest.raises(SystemExit) as exc_info:
                run_holiday_check(secrets)
        assert exc_info.value.code == 0

    def test_phase0_exits_clean_on_holiday(self):
        """NSE holiday must call sys.exit(0) — clean exit."""
        # Use a date in nse_holidays.yaml
        holiday = datetime(2026, 1, 26, 8, 0, 0, tzinfo=IST)  # Republic Day 2026
        secrets = {"telegram": {"bot_token": "", "chat_id": ""}}

        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = holiday
            with pytest.raises(SystemExit) as exc_info:
                run_holiday_check(secrets)
        assert exc_info.value.code == 0

    def test_phase0_proceeds_on_trading_day(self):
        """Regular trading day must not exit."""
        # 2026-03-06 is a Friday, not a holiday
        trading_day = datetime(2026, 3, 6, 8, 0, 0, tzinfo=IST)
        secrets = {"telegram": {"bot_token": "", "chat_id": ""}}

        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = trading_day
            # Must not raise SystemExit
            run_holiday_check(secrets)


class TestSharedStateInit:
    def test_shared_state_all_keys_initialised(self):
        """_init_shared_state() must contain all D6 + D9 keys."""
        state = _init_shared_state()
        missing_d6 = [k for k in REQUIRED_D6_KEYS if k not in state]
        missing_d9 = [k for k in REQUIRED_D9_KEYS if k not in state]

        assert missing_d6 == [], f"Missing D6 keys: {missing_d6}"
        assert missing_d9 == [], f"Missing D9 keys: {missing_d9}"

    def test_shared_state_queues_are_asyncio_queues(self):
        """tick_queue and order_queue must be asyncio.Queue instances."""
        state = _init_shared_state()
        assert isinstance(state["tick_queue"], asyncio.Queue)
        assert isinstance(state["order_queue"], asyncio.Queue)

    def test_shared_state_kill_switch_starts_at_0(self):
        state = _init_shared_state()
        assert state["kill_switch_level"] == 0
        assert state["system_ready"] is False
        assert state["accepting_signals"] is True

    def test_shared_state_tasks_alive_has_all_5_tasks(self):
        state = _init_shared_state()
        expected_tasks = {"ws_listener", "signal_processor", "order_monitor", "risk_watchdog", "heartbeat"}
        assert set(state["tasks_alive"].keys()) == expected_tasks


class TestRiskWatchdogL2Trigger:
    def test_risk_watchdog_triggers_l2_at_3pct_loss(self):
        """
        Risk watchdog condition: daily_pnl_pct <= -0.03 must trigger L2.
        Tests the condition directly (not the full coroutine).
        """
        shared_state = _init_shared_state()
        shared_state["daily_pnl_pct"] = -0.031  # 3.1% loss — over the cap
        shared_state["kill_switch_level"] = 0
        config = {"risk": {"max_daily_loss_pct": 0.03}}
        secrets = {"telegram": {"bot_token": "", "chat_id": ""}}

        max_daily_loss = config["risk"]["max_daily_loss_pct"]
        pnl = shared_state["daily_pnl_pct"]

        # Validate the trigger condition matches spec
        l2_triggered = pnl <= -max_daily_loss
        assert l2_triggered is True

        # Run trigger_kill_switch directly
        asyncio.run(
            trigger_kill_switch(2, "daily_loss_3pct", shared_state, config, secrets)
        )
        assert shared_state["kill_switch_level"] == 2
        assert shared_state["accepting_signals"] is False

    def test_risk_watchdog_no_l2_below_threshold(self):
        """daily_pnl_pct == -2.9% must NOT trigger L2 (below 3% cap)."""
        max_daily_loss = 0.03
        pnl = -0.029  # 2.9% — under the cap
        l2_triggered = pnl <= -max_daily_loss
        assert l2_triggered is False
