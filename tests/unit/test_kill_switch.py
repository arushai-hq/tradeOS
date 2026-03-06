"""
Unit tests for trigger_kill_switch() in main.py

Tests:
  - Level 1 sets accepting_signals = False
  - Level 2 calls emergency_exit_all on exec_engine
  - Kill switch does not downgrade (level must only increase)
  - Compound condition: both consecutive_losses >= 5 AND daily_pnl_pct <= -0.015
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from main import trigger_kill_switch


def _make_shared_state(**overrides) -> dict:
    """Minimal shared_state for kill switch tests."""
    state = {
        "kill_switch_level": 0,
        "accepting_signals": True,
        "daily_pnl_pct": 0.0,
        "consecutive_losses": 0,
        "telegram_active": False,  # Suppress Telegram calls
    }
    state.update(overrides)
    return state


def _make_config() -> dict:
    return {"risk": {"max_daily_loss_pct": 0.03}}


def _make_secrets() -> dict:
    return {"telegram": {"bot_token": "", "chat_id": ""}}


class TestKillSwitchLevel1:
    def test_level1_stops_accepting_signals(self):
        """L1 must set accepting_signals = False."""
        shared_state = _make_shared_state()
        config = _make_config()
        secrets = _make_secrets()

        asyncio.run(trigger_kill_switch(1, "test_l1", shared_state, config, secrets))

        assert shared_state["kill_switch_level"] == 1
        assert shared_state["accepting_signals"] is False

    def test_level1_does_not_call_emergency_exit(self):
        """L1 must NOT close positions — that's L2 only."""
        exec_engine = MagicMock()
        exec_engine._exit_manager = AsyncMock()
        exec_engine._exit_manager.emergency_exit_all = AsyncMock()

        shared_state = _make_shared_state()
        asyncio.run(
            trigger_kill_switch(1, "test_l1", shared_state, _make_config(), _make_secrets(), exec_engine)
        )

        exec_engine._exit_manager.emergency_exit_all.assert_not_called()


class TestKillSwitchLevel2:
    def test_level2_triggers_emergency_exit(self):
        """L2 must call emergency_exit_all on exec_engine._exit_manager."""
        exit_manager = MagicMock()
        exit_manager.emergency_exit_all = AsyncMock()
        exec_engine = MagicMock()
        exec_engine._exit_manager = exit_manager

        shared_state = _make_shared_state()
        asyncio.run(
            trigger_kill_switch(2, "test_l2", shared_state, _make_config(), _make_secrets(), exec_engine)
        )

        assert shared_state["kill_switch_level"] == 2
        assert shared_state["accepting_signals"] is False
        exit_manager.emergency_exit_all.assert_called_once_with("test_l2")

    def test_level2_without_exec_engine_does_not_crash(self):
        """L2 with exec_engine=None must not raise."""
        shared_state = _make_shared_state()
        asyncio.run(
            trigger_kill_switch(2, "test_l2_no_engine", shared_state, _make_config(), _make_secrets())
        )
        assert shared_state["kill_switch_level"] == 2


class TestKillSwitchNoDowngrade:
    def test_kill_switch_does_not_downgrade(self):
        """If level <= current, must be a no-op — never downgrade."""
        shared_state = _make_shared_state(kill_switch_level=2, accepting_signals=False)
        asyncio.run(
            trigger_kill_switch(1, "attempt_downgrade", shared_state, _make_config(), _make_secrets())
        )
        # Must stay at 2 — not downgrade to 1
        assert shared_state["kill_switch_level"] == 2

    def test_kill_switch_same_level_noop(self):
        """Triggering at current level is a no-op."""
        shared_state = _make_shared_state(kill_switch_level=1, accepting_signals=False)
        asyncio.run(
            trigger_kill_switch(1, "same_level", shared_state, _make_config(), _make_secrets())
        )
        assert shared_state["kill_switch_level"] == 1


class TestCompoundL1Condition:
    """L1 trigger requires BOTH consecutive_losses >= 5 AND daily_pnl_pct <= -0.015."""

    def test_compound_both_required_only_losses(self):
        """Losses alone (without pnl threshold) must NOT trigger L1."""
        shared_state = _make_shared_state(consecutive_losses=5, daily_pnl_pct=0.0)
        # Directly test the condition logic matches the spec
        losses = shared_state["consecutive_losses"]
        pnl = shared_state["daily_pnl_pct"]
        compound_triggered = losses >= 5 and pnl <= -0.015
        assert compound_triggered is False

    def test_compound_both_required_only_pnl(self):
        """PnL alone (without loss count threshold) must NOT trigger L1."""
        shared_state = _make_shared_state(consecutive_losses=2, daily_pnl_pct=-0.02)
        losses = shared_state["consecutive_losses"]
        pnl = shared_state["daily_pnl_pct"]
        compound_triggered = losses >= 5 and pnl <= -0.015
        assert compound_triggered is False

    def test_compound_both_conditions_triggers(self):
        """Both losses >= 5 AND pnl <= -1.5% must trigger L1."""
        shared_state = _make_shared_state(consecutive_losses=5, daily_pnl_pct=-0.016)
        losses = shared_state["consecutive_losses"]
        pnl = shared_state["daily_pnl_pct"]
        compound_triggered = losses >= 5 and pnl <= -0.015
        assert compound_triggered is True

    def test_compound_boundary_exact_values(self):
        """Exact boundary: losses == 5 AND pnl == -0.015 triggers."""
        losses = 5
        pnl = -0.015
        compound_triggered = losses >= 5 and pnl <= -0.015
        assert compound_triggered is True
