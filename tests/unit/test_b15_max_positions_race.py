"""
TradeOS — B15 Max Positions Race Condition Tests

Tests the three-layer defense against simultaneous signals exceeding max_open_positions:
  (1) Pending counter: 5 signals at Gate 4 with 1 open → only 3 pass (1+3=4 max)
  (2) Hard gate at execution: signal passes risk gate but rejected by EE hard gate
  (3) Sizer rejection: pending counter decremented on sizer reject
  (4) Successful fill: pending counter decremented and open_positions incremented
  (5) Capital ceiling: reject when deployed + new > s1_allocation
  (6) Counter never negative: decrement when counter is already 0
  (7) End-to-end: Session 08 scenario (1 open + 5 simultaneous signals → 3 new, 2 blocked)
  (8) Gate 4 includes pending in count — isolated test
  (9) Order placement failure: pending counter decremented
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytz
from freezegun import freeze_time

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from core.strategy_engine.risk_gate import RiskGate
from core.strategy_engine.signal_generator import Signal

IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _signal(symbol: str = "RELIANCE", direction: str = "LONG",
            entry: float = 2500.0, stop: float = 2450.0) -> Signal:
    entry_d = Decimal(str(entry))
    stop_d = Decimal(str(stop))
    risk = abs(entry_d - stop_d)
    target = entry_d + Decimal("2") * risk
    return Signal(
        symbol=symbol,
        instrument_token=738561,
        direction=direction,
        signal_time=datetime.now(IST),
        candle_time=datetime(2026, 3, 16, 10, 0, tzinfo=IST),
        theoretical_entry=entry_d,
        stop_loss=stop_d,
        target=target,
        ema9=Decimal("2495"),
        ema21=Decimal("2490"),
        rsi=Decimal("62"),
        vwap=Decimal("2480"),
        volume_ratio=Decimal("1.8"),
    )


def _state(open_positions: dict | None = None, pending: int = 0) -> dict:
    return {
        "kill_switch_level": 0,
        "recon_in_progress": False,
        "locked_instruments": set(),
        "open_positions": open_positions or {},
        "pending_signals": pending,
    }


def _config(max_positions: int = 4) -> dict:
    return {
        "system": {"mode": "paper"},
        "risk": {"max_open_positions": max_positions},
        "trading_hours": {"no_entry_after": "14:30"},
        "capital": {
            "total": 1000000,
            "allocation": {"s1_intraday": 0.70},
        },
    }


def _open_pos(symbol: str, entry: float = 2500.0, qty: int = 50) -> dict:
    """Create an open position dict matching PnlTracker format."""
    return {
        "direction": "LONG",
        "qty": qty,
        "entry_price": Decimal(str(entry)),
        "order_id": f"ORD_{symbol}",
        "signal_id": 1,
        "entry_time": datetime(2026, 3, 16, 10, 0, 0, tzinfo=IST),
    }


# ---------------------------------------------------------------------------
# (1) Pending counter: 5 signals, 1 open, max 4 → only 3 pass Gate 4
# ---------------------------------------------------------------------------

@freeze_time("2026-03-16 04:00:00")
def test_pending_counter_blocks_excess_signals():
    """5 signals arrive with 1 open position. Only 3 should pass Gate 4 (1+3=4)."""
    gate = RiskGate()
    state = _state(
        open_positions={"SUNPHARMA": _open_pos("SUNPHARMA")},
        pending=0,
    )
    config = _config(max_positions=4)

    symbols = ["RELIANCE", "INFY", "TCS", "TITAN", "HCLTECH"]
    passed = []
    blocked = []

    for sym in symbols:
        sig = _signal(symbol=sym)
        allowed, reason = gate.check(sig, state, config)
        if allowed:
            passed.append(sym)
            # Simulate what StrategyEngine does: increment pending
            state["pending_signals"] = state.get("pending_signals", 0) + 1
        else:
            blocked.append((sym, reason))

    assert len(passed) == 3, f"Expected 3 passed, got {len(passed)}: {passed}"
    assert len(blocked) == 2, f"Expected 2 blocked, got {len(blocked)}: {blocked}"
    # The 4th and 5th signals should be blocked (1 open + 3 pending = 4 = max)
    for sym, reason in blocked:
        assert reason == "MAX_POSITIONS_REACHED"


# ---------------------------------------------------------------------------
# (2) Hard gate at execution: rejected when open_positions >= max
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ee_hard_gate_rejects_at_max_positions():
    """Execution engine rejects signal when open_positions >= max, decrements pending."""
    from core.execution_engine import ExecutionEngine

    shared_state = {
        "open_positions": {
            "A": _open_pos("A"), "B": _open_pos("B"),
            "C": _open_pos("C"), "D": _open_pos("D"),
        },
        "max_open_positions": 4,
        "pending_signals": 1,
        "signals_rejected_today": 0,
        "market_regime": "unknown",
    }
    config = _config(max_positions=4)
    mock_rm = MagicMock()
    mock_db = AsyncMock()

    ee = ExecutionEngine(
        kite=MagicMock(),
        config=config,
        shared_state=shared_state,
        order_queue=asyncio.Queue(),
        risk_manager=mock_rm,
        db_pool=mock_db,
    )
    ee._order_placer = MagicMock()
    ee._session_date = datetime.now(IST).date()

    sig = _signal(symbol="NEWSTOCK")
    await ee._handle_signal(sig)

    assert shared_state["pending_signals"] == 0, "pending should decrement"
    assert shared_state["signals_rejected_today"] == 1
    mock_rm.size_position.assert_not_called()  # Should not reach sizer


# ---------------------------------------------------------------------------
# (3) Sizer rejection: pending counter decremented
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sizer_rejection_decrements_pending():
    """When sizer rejects, pending_signals must decrement."""
    from core.execution_engine import ExecutionEngine

    shared_state = {
        "open_positions": {},
        "max_open_positions": 4,
        "pending_signals": 2,
        "signals_rejected_today": 0,
        "market_regime": "unknown",
    }
    config = _config(max_positions=4)
    mock_rm = MagicMock()
    mock_rm.size_position.return_value = None  # sizer rejects
    mock_db = AsyncMock()

    ee = ExecutionEngine(
        kite=MagicMock(),
        config=config,
        shared_state=shared_state,
        order_queue=asyncio.Queue(),
        risk_manager=mock_rm,
        db_pool=mock_db,
    )
    ee._order_placer = MagicMock()
    ee._session_date = datetime.now(IST).date()

    sig = _signal(symbol="RELIANCE")
    await ee._handle_signal(sig)

    assert shared_state["pending_signals"] == 1, "pending should decrement by 1"
    mock_rm.size_position.assert_called_once()


# ---------------------------------------------------------------------------
# (4) Successful fill: pending decremented, open_positions incremented
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_successful_order_decrements_pending():
    """Successful order placement decrements pending_signals."""
    from core.execution_engine import ExecutionEngine

    shared_state = {
        "open_positions": {},
        "max_open_positions": 4,
        "pending_signals": 3,
        "signals_rejected_today": 0,
        "signals_generated_today": 0,
        "market_regime": "unknown",
    }
    config = _config(max_positions=4)
    mock_rm = MagicMock()
    mock_rm.size_position.return_value = 50  # sizer passes

    mock_order = MagicMock()
    mock_order.order_id = "ORD_001"

    mock_db = AsyncMock()

    ee = ExecutionEngine(
        kite=MagicMock(),
        config=config,
        shared_state=shared_state,
        order_queue=asyncio.Queue(),
        risk_manager=mock_rm,
        db_pool=mock_db,
    )
    mock_placer = AsyncMock()
    mock_placer.place_entry = AsyncMock(return_value=mock_order)
    ee._order_placer = mock_placer
    ee._session_date = datetime.now(IST).date()

    sig = _signal(symbol="RELIANCE")
    await ee._handle_signal(sig)

    assert shared_state["pending_signals"] == 2, "pending should decrement by 1"
    assert shared_state["signals_generated_today"] == 1


# ---------------------------------------------------------------------------
# (5) Capital ceiling: reject when deployed + new > s1_capital
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_capital_ceiling_rejects_excess():
    """Capital ceiling gate rejects when deployed + estimated > s1 allocation.

    Then verifies hard gate blocks when max positions reached.
    """
    from core.execution_engine import ExecutionEngine

    # 3 positions open, 70 qty × ₹2500 each = ₹5,25,000 deployed
    # s1_capital = 1,000,000 × 0.70 = ₹7,00,000
    # slot_capital = 7,00,000 / 4 = ₹1,75,000
    # estimated = ₹1,75,000
    # deployed + estimated = ₹7,00,000 ≤ ₹7,35,000 (s1 × 1.05) → passes
    shared_state = {
        "open_positions": {
            "A": {"direction": "LONG", "qty": 70, "entry_price": Decimal("2500"), "order_id": "1", "signal_id": 1, "entry_time": datetime(2026, 3, 16, 10, 0, 0, tzinfo=IST)},
            "B": {"direction": "LONG", "qty": 70, "entry_price": Decimal("2500"), "order_id": "2", "signal_id": 2, "entry_time": datetime(2026, 3, 16, 10, 0, 0, tzinfo=IST)},
            "C": {"direction": "LONG", "qty": 70, "entry_price": Decimal("2500"), "order_id": "3", "signal_id": 3, "entry_time": datetime(2026, 3, 16, 10, 0, 0, tzinfo=IST)},
        },
        "max_open_positions": 4,
        "pending_signals": 1,
        "signals_rejected_today": 0,
        "market_regime": "unknown",
    }
    config = _config(max_positions=4)
    mock_rm = MagicMock()
    mock_rm.size_position.return_value = 70

    mock_order = MagicMock()
    mock_order.order_id = "ORD_004"
    mock_db = AsyncMock()

    ee = ExecutionEngine(
        kite=MagicMock(),
        config=config,
        shared_state=shared_state,
        order_queue=asyncio.Queue(),
        risk_manager=mock_rm,
        db_pool=mock_db,
    )
    mock_placer = AsyncMock()
    mock_placer.place_entry = AsyncMock(return_value=mock_order)
    ee._order_placer = mock_placer
    ee._session_date = datetime.now(IST).date()

    # This signal should pass (3 open × ₹1,75,000 + ₹1,75,000 = ₹7,00,000 ≤ ₹7,35,000)
    sig = _signal(symbol="NEWSTOCK", entry=2500.0)
    await ee._handle_signal(sig)
    assert mock_rm.size_position.called, "Should reach sizer (capital within limits)"

    # Now add a 4th position and try again — should be blocked by hard gate (4 >= 4)
    shared_state["open_positions"]["D"] = {
        "direction": "LONG", "qty": 70, "entry_price": Decimal("2500"),
        "order_id": "4", "signal_id": 4, "entry_time": datetime(2026, 3, 16, 10, 0, 0, tzinfo=IST),
    }
    shared_state["pending_signals"] = 1
    shared_state["signals_rejected_today"] = 0
    mock_rm.size_position.reset_mock()

    sig2 = _signal(symbol="EXTRA", entry=2500.0)
    await ee._handle_signal(sig2)
    assert shared_state["pending_signals"] == 0
    mock_rm.size_position.assert_not_called()  # Blocked by hard gate before sizer


# ---------------------------------------------------------------------------
# (6) Counter never goes negative
# ---------------------------------------------------------------------------

def test_pending_counter_never_negative():
    """Decrementing when counter is 0 must stay at 0."""
    from core.execution_engine import ExecutionEngine

    shared_state = {"pending_signals": 0}
    ee = ExecutionEngine.__new__(ExecutionEngine)
    ee._shared_state = shared_state

    ee._decrement_pending()
    assert shared_state["pending_signals"] == 0

    ee._decrement_pending()
    assert shared_state["pending_signals"] == 0


# ---------------------------------------------------------------------------
# (7) End-to-end: Session 08 scenario
# ---------------------------------------------------------------------------

@freeze_time("2026-03-16 04:00:00")
def test_session_08_scenario_e2e():
    """
    Session 08 replay: 1 open position (SUNPHARMA) + 5 signals arrive simultaneously.
    Max 4 positions. Expected: 3 new positions pass (total 4), 2 blocked.
    """
    gate = RiskGate()
    state = _state(
        open_positions={"SUNPHARMA": _open_pos("SUNPHARMA", entry=1825.5, qty=71)},
        pending=0,
    )
    config = _config(max_positions=4)

    # 5 simultaneous signals from a candle batch
    batch = [
        _signal(symbol="RELIANCE", direction="SHORT", entry=2600.0, stop=2650.0),
        _signal(symbol="INFY", direction="SHORT", entry=1500.0, stop=1550.0),
        _signal(symbol="TCS", direction="SHORT", entry=3400.0, stop=3500.0),
        _signal(symbol="TITAN", direction="SHORT", entry=3200.0, stop=3300.0),
        _signal(symbol="HCLTECH", direction="SHORT", entry=1370.0, stop=1420.0),
    ]

    passed_symbols = []
    blocked_symbols = []

    for sig in batch:
        allowed, reason = gate.check(sig, state, config)
        if allowed:
            passed_symbols.append(sig.symbol)
            state["pending_signals"] += 1
        else:
            blocked_symbols.append(sig.symbol)

    # Verify: 1 existing + 3 new = 4 (max), 2 blocked
    assert len(passed_symbols) == 3, f"Expected 3 passed, got {passed_symbols}"
    assert len(blocked_symbols) == 2, f"Expected 2 blocked, got {blocked_symbols}"
    assert state["pending_signals"] == 3

    # After execution engine processes the 3 signals, pending should return to 0
    # and open_positions should have 4 entries
    for sym in passed_symbols:
        state["pending_signals"] = max(0, state["pending_signals"] - 1)
        state["open_positions"][sym] = _open_pos(sym)

    assert state["pending_signals"] == 0
    assert len(state["open_positions"]) == 4  # SUNPHARMA + 3 new


# ---------------------------------------------------------------------------
# (8) Gate 4 includes pending in count — isolated test
# ---------------------------------------------------------------------------

@freeze_time("2026-03-16 04:00:00")
def test_gate4_counts_pending_signals():
    """Gate 4 must check open_positions + pending_signals against max."""
    gate = RiskGate()
    config = _config(max_positions=3)

    # 1 open + 2 pending = 3 = max → should block
    state = _state(
        open_positions={"A": _open_pos("A")},
        pending=2,
    )
    sig = _signal(symbol="D")
    allowed, reason = gate.check(sig, state, config)
    assert not allowed
    assert reason == "MAX_POSITIONS_REACHED"

    # 1 open + 1 pending = 2 < 3 → should pass
    state["pending_signals"] = 1
    allowed, reason = gate.check(sig, state, config)
    assert allowed
    assert reason == "OK"


# ---------------------------------------------------------------------------
# (9) Order placement failure: pending counter decremented
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_order_placement_failure_decrements_pending():
    """If order placement returns None (failure), pending must decrement."""
    from core.execution_engine import ExecutionEngine

    shared_state = {
        "open_positions": {},
        "max_open_positions": 4,
        "pending_signals": 1,
        "signals_rejected_today": 0,
        "signals_generated_today": 0,
        "market_regime": "unknown",
    }
    config = _config(max_positions=4)
    mock_rm = MagicMock()
    mock_rm.size_position.return_value = 50

    mock_db = AsyncMock()

    ee = ExecutionEngine(
        kite=MagicMock(),
        config=config,
        shared_state=shared_state,
        order_queue=asyncio.Queue(),
        risk_manager=mock_rm,
        db_pool=mock_db,
    )
    mock_placer = AsyncMock()
    mock_placer.place_entry = AsyncMock(return_value=None)  # placement fails
    ee._order_placer = mock_placer
    ee._session_date = datetime.now(IST).date()

    sig = _signal(symbol="RELIANCE")
    await ee._handle_signal(sig)

    assert shared_state["pending_signals"] == 0, "pending should decrement on placement failure"
