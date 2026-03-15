"""
Unit tests for risk_manager.loss_tracker.LossTracker.

D8 mandatory test catalogue:
  test_consecutive_loss_counter_resets_on_win
  test_consecutive_losses_written_to_shared_state
  test_session_start_resets_counter
  test_kill_switch_reset_resets_counter
  test_breakeven_trade_treated_as_win
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from core.risk_manager.loss_tracker import LossTracker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def state() -> dict:
    return {"consecutive_losses": 0}


@pytest.fixture
def tracker(state: dict) -> LossTracker:
    return LossTracker(shared_state=state)


# ---------------------------------------------------------------------------
# test_consecutive_loss_counter_resets_on_win
# ---------------------------------------------------------------------------

def test_consecutive_loss_counter_resets_on_win(tracker: LossTracker):
    """3 losses → counter=3, then 1 win → counter=0."""
    tracker.on_trade_close(Decimal("-100"))
    tracker.on_trade_close(Decimal("-200"))
    tracker.on_trade_close(Decimal("-50"))
    assert tracker.get_count() == 3

    # Win resets counter
    tracker.on_trade_close(Decimal("500"))
    assert tracker.get_count() == 0


def test_counter_increments_on_each_loss(tracker: LossTracker):
    """Each loss increments by exactly 1."""
    for i in range(1, 6):
        tracker.on_trade_close(Decimal("-1"))
        assert tracker.get_count() == i


def test_counter_stays_zero_on_win(tracker: LossTracker):
    """Counter should stay at 0 if only wins occur."""
    tracker.on_trade_close(Decimal("100"))
    tracker.on_trade_close(Decimal("200"))
    assert tracker.get_count() == 0


# ---------------------------------------------------------------------------
# test_consecutive_losses_written_to_shared_state
# ---------------------------------------------------------------------------

def test_consecutive_losses_written_to_shared_state(
    tracker: LossTracker, state: dict
):
    """shared_state["consecutive_losses"] must mirror the internal counter."""
    tracker.on_trade_close(Decimal("-100"))
    assert state["consecutive_losses"] == 1

    tracker.on_trade_close(Decimal("-200"))
    assert state["consecutive_losses"] == 2

    # Win resets shared_state too
    tracker.on_trade_close(Decimal("50"))
    assert state["consecutive_losses"] == 0


def test_shared_state_updated_on_every_event(tracker: LossTracker, state: dict):
    """Each call must update shared_state, not just at end."""
    tracker.on_trade_close(Decimal("-1"))
    assert state["consecutive_losses"] == 1

    tracker.on_trade_close(Decimal("-1"))
    assert state["consecutive_losses"] == 2

    tracker.on_trade_close(Decimal("1"))
    assert state["consecutive_losses"] == 0


# ---------------------------------------------------------------------------
# test_session_start_resets_counter
# ---------------------------------------------------------------------------

def test_session_start_resets_counter(tracker: LossTracker, state: dict):
    """on_session_start() must reset counter to 0 regardless of current value."""
    # Accumulate losses
    tracker.on_trade_close(Decimal("-100"))
    tracker.on_trade_close(Decimal("-100"))
    tracker.on_trade_close(Decimal("-100"))
    assert tracker.get_count() == 3

    tracker.on_session_start()

    assert tracker.get_count() == 0
    assert state["consecutive_losses"] == 0


def test_session_start_resets_shared_state(tracker: LossTracker, state: dict):
    """on_session_start() must write 0 to shared_state."""
    # Pre-set a non-zero value
    state["consecutive_losses"] = 5
    tracker.on_session_start()
    assert state["consecutive_losses"] == 0


# ---------------------------------------------------------------------------
# test_kill_switch_reset_resets_counter
# ---------------------------------------------------------------------------

def test_kill_switch_reset_resets_counter(tracker: LossTracker, state: dict):
    """
    Critical gap fix: after manual kill switch reset, counter must be 0.
    Without this, the kill switch would immediately re-trigger because
    shared_state["consecutive_losses"] still == 5+.
    """
    # Simulate 5 consecutive losses (enough to trigger L1 if pnl < -1.5%)
    for _ in range(5):
        tracker.on_trade_close(Decimal("-1000"))

    assert tracker.get_count() == 5
    assert state["consecutive_losses"] == 5

    # Manual kill switch reset is issued
    tracker.on_kill_switch_reset()

    assert tracker.get_count() == 0
    assert state["consecutive_losses"] == 0


def test_kill_switch_reset_then_new_losses(tracker: LossTracker, state: dict):
    """After kill switch reset, counter starts fresh from 0."""
    for _ in range(3):
        tracker.on_trade_close(Decimal("-1"))

    tracker.on_kill_switch_reset()
    assert tracker.get_count() == 0

    # New losses after reset start from 0
    tracker.on_trade_close(Decimal("-1"))
    assert tracker.get_count() == 1
    assert state["consecutive_losses"] == 1


# ---------------------------------------------------------------------------
# test_breakeven_trade_treated_as_win
# ---------------------------------------------------------------------------

def test_breakeven_trade_treated_as_win(tracker: LossTracker, state: dict):
    """net_pnl = 0 (breakeven after charges) → counter resets to 0."""
    tracker.on_trade_close(Decimal("-100"))
    tracker.on_trade_close(Decimal("-100"))
    assert tracker.get_count() == 2

    # Breakeven trade (net_pnl == 0)
    tracker.on_trade_close(Decimal("0"))

    assert tracker.get_count() == 0
    assert state["consecutive_losses"] == 0


def test_very_small_positive_pnl_treated_as_win(tracker: LossTracker):
    """Any positive net_pnl (even ₹0.01) resets the counter."""
    tracker.on_trade_close(Decimal("-500"))
    tracker.on_trade_close(Decimal("-500"))

    tracker.on_trade_close(Decimal("0.01"))

    assert tracker.get_count() == 0


# ---------------------------------------------------------------------------
# Additional: get_count reflects state
# ---------------------------------------------------------------------------

def test_get_count_reflects_current_state(tracker: LossTracker):
    """get_count() must always match the internal counter."""
    assert tracker.get_count() == 0

    tracker.on_trade_close(Decimal("-1"))
    assert tracker.get_count() == 1

    tracker.on_trade_close(Decimal("1"))
    assert tracker.get_count() == 0
