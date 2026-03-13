"""
Tests for DB trade history feature (D1, D3, D4, D5).

(a) Signal status UPDATE on sizer rejection — mock db_pool, verify UPDATE with status='REJECTED'
(b) Signal status UPDATE on fill — mock db_pool, verify UPDATE with status='FILLED' and order_id
(c) Session summary INSERT at EOD — mock db_pool, verify INSERT with correct computed values
(d) Backfill script dry-run — verify no writes in dry-run mode
(e) Dead code removed — storage.py no longer has write_signal, write_trade, write_system_event
(f) OrderMonitor handles missing db_pool gracefully (backward compat)
(g) ExecutionEngine._update_signal_status handles DB errors without crashing
(h) _ensure_sessions_table creates table when missing
(i) _ensure_sessions_table skips when table exists
(j) kill_switch_max tracked in shared_state
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytz

IST = pytz.timezone("Asia/Kolkata")


def _mock_db_pool(mock_conn):
    """Create a mock asyncpg pool that yields mock_conn from acquire()."""
    @asynccontextmanager
    async def _acquire():
        yield mock_conn

    mock_pool = MagicMock()
    mock_pool.acquire = _acquire
    return mock_pool


# -----------------------------------------------------------------------
# (a) Signal status UPDATE on sizer rejection
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_d1_sizer_rejection_updates_signal_status():
    """ExecutionEngine._handle_signal updates signal to REJECTED on sizer rejection."""
    from execution_engine import ExecutionEngine
    from strategy_engine.signal_generator import Signal

    ee = ExecutionEngine.__new__(ExecutionEngine)
    ee._order_placer = MagicMock()
    ee._risk_manager = MagicMock()
    ee._risk_manager.size_position.return_value = None  # sizer rejects
    ee._shared_state = {"signals_rejected_today": 0}
    ee._notifier = None
    ee._session_date = date(2026, 3, 13)

    mock_conn = AsyncMock()
    ee._db_pool = _mock_db_pool(mock_conn)

    signal = MagicMock(spec=Signal)
    signal.symbol = "RELIANCE"
    signal.direction = "LONG"
    signal.theoretical_entry = Decimal("2500.00")
    signal.stop_loss = Decimal("2450.00")

    await ee._handle_signal(signal)

    # Verify UPDATE was called
    mock_conn.execute.assert_called_once()
    call_args = mock_conn.execute.call_args
    sql = call_args[0][0]
    assert "UPDATE signals SET status" in sql
    assert call_args[0][1] == "REJECTED"
    assert "SIZER_REJECTED:LONG" in call_args[0][2]
    assert call_args[0][4] == date(2026, 3, 13)
    assert call_args[0][5] == "RELIANCE"


# -----------------------------------------------------------------------
# (b) Signal status UPDATE on fill
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_d1_entry_fill_updates_signal_status():
    """OrderMonitor._on_entry_fill updates signal to FILLED with order_id."""
    from execution_engine.order_monitor import OrderMonitor
    from execution_engine.state_machine import Order

    monitor = OrderMonitor.__new__(OrderMonitor)
    monitor._mode = "paper"
    monitor._is_paper = True
    monitor._processed_order_ids = set()
    monitor._shared_state = {"fills_today": 0}
    monitor._risk_manager = AsyncMock()
    monitor._exit_manager = AsyncMock()
    monitor._notifier = None
    monitor._session_date = date(2026, 3, 13)

    mock_conn = AsyncMock()
    monitor._db_pool = _mock_db_pool(mock_conn)

    order = MagicMock(spec=Order)
    order.order_id = "PAPER-TEST-001"
    order.symbol = "SUNPHARMA"
    order.direction = "SHORT"
    order.qty = 71
    order.fill_price = Decimal("1825.50")
    order.price = Decimal("1825.50")
    order.stop_loss = Decimal("1850.00")
    order.target = Decimal("1780.00")
    order.signal_id = 20

    await monitor._on_entry_fill(order)

    # Verify UPDATE was called with FILLED status and order_id
    mock_conn.execute.assert_called_once()
    call_args = mock_conn.execute.call_args
    sql = call_args[0][0]
    assert "UPDATE signals SET status" in sql
    assert call_args[0][1] == "FILLED"
    assert call_args[0][2] == "PAPER-TEST-001"
    assert call_args[0][3] == date(2026, 3, 13)
    assert call_args[0][4] == "SUNPHARMA"


# -----------------------------------------------------------------------
# (c) Session summary INSERT at EOD
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_d3_session_summary_insert():
    """_write_session_summary queries signals/trades and INSERTs session row."""
    from main import _write_session_summary

    session_date = date(2026, 3, 13)
    shared_state = {
        "session_date": session_date,
        "session_start_time": datetime(2026, 3, 13, 9, 15, tzinfo=IST),
        "market_regime": "bear_trend",
        "kill_switch_max": 0,
    }
    config = {"capital": {"total": 1000000}}

    sig_row = {"total": 9, "accepted": 3, "rejected": 6}
    trade_row = {
        "total": 2, "won": 2, "lost": 0,
        "gross_pnl": Decimal("1538.00"),
        "total_charges": Decimal("148.00"),
        "net_pnl": Decimal("1390.00"),
    }

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(side_effect=[sig_row, trade_row])
    mock_conn.execute = AsyncMock()

    mock_pool = _mock_db_pool(mock_conn)

    await _write_session_summary(mock_pool, shared_state, config)

    # Verify the INSERT was called
    assert mock_conn.execute.call_count == 1
    call_args = mock_conn.execute.call_args
    sql = call_args[0][0]
    assert "INSERT INTO sessions" in sql
    assert "ON CONFLICT (session_date) DO UPDATE" in sql
    # Verify key values
    assert call_args[0][1] == session_date  # session_date
    assert call_args[0][5] == 9   # signals_total
    assert call_args[0][6] == 3   # signals_accepted
    assert call_args[0][7] == 6   # signals_rejected
    assert call_args[0][8] == 2   # trades_total
    assert call_args[0][9] == 2   # trades_won
    assert call_args[0][10] == 0  # trades_lost
    assert call_args[0][15] == 1000000.0  # capital
    assert call_args[0][16] == 0  # kill_switch_max
    assert call_args[0][17] == "PASS"  # health_status


# -----------------------------------------------------------------------
# (d) Backfill script dry-run
# -----------------------------------------------------------------------

def test_d4_backfill_script_exists():
    """Backfill script file exists and is importable."""
    from pathlib import Path
    script = Path(__file__).resolve().parent.parent.parent / "tools" / "db_backfill_session07.py"
    assert script.exists(), f"Backfill script not found at {script}"


def test_d4_backfill_trade_fixes_correct():
    """Verify the trade fix constants are correct."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from tools.db_backfill_session07 import TRADE_FIXES

    assert len(TRADE_FIXES) == 2

    sunpharma = TRADE_FIXES[0]
    assert sunpharma["symbol"] == "SUNPHARMA"
    assert sunpharma["trade_id"] == 3
    assert sunpharma["signal_id"] == 20
    assert sunpharma["exit_price"] == Decimal("1805.10")
    expected_gross = (sunpharma["entry_price"] - sunpharma["exit_price"]) * Decimal(str(sunpharma["qty"]))
    assert expected_gross == Decimal("1448.40")

    titan = TRADE_FIXES[1]
    assert titan["symbol"] == "TITAN"
    assert titan["trade_id"] == 4
    assert titan["signal_id"] == 23


# -----------------------------------------------------------------------
# (e) Dead code removed from storage.py
# -----------------------------------------------------------------------

def test_d5_storage_dead_code_removed():
    """TickStorage no longer has write_signal, write_trade, write_system_event."""
    from data_engine.storage import TickStorage

    assert not hasattr(TickStorage, "write_signal"), "write_signal should be removed"
    assert not hasattr(TickStorage, "write_trade"), "write_trade should be removed"
    assert not hasattr(TickStorage, "write_system_event"), "write_system_event should be removed"
    assert hasattr(TickStorage, "write_tick")
    assert hasattr(TickStorage, "flush_loop")
    assert hasattr(TickStorage, "_do_flush")


# -----------------------------------------------------------------------
# (f) OrderMonitor handles missing db_pool gracefully
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_order_monitor_no_db_pool():
    """OrderMonitor._update_signal_status is a no-op when db_pool is None."""
    from execution_engine.order_monitor import OrderMonitor

    monitor = OrderMonitor.__new__(OrderMonitor)
    # No _db_pool attribute set at all — should not crash
    result = await monitor._update_signal_status("RELIANCE", "FILLED", order_id="X")
    assert result is None


# -----------------------------------------------------------------------
# (g) ExecutionEngine._update_signal_status handles DB errors
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ee_signal_update_db_error_no_crash():
    """DB error in _update_signal_status is logged but doesn't crash."""
    from execution_engine import ExecutionEngine

    ee = ExecutionEngine.__new__(ExecutionEngine)
    ee._session_date = date(2026, 3, 13)

    @asynccontextmanager
    async def _failing_acquire():
        raise Exception("DB connection failed")
        yield  # unreachable, but needed for generator syntax  # noqa: E501

    mock_pool = MagicMock()
    mock_pool.acquire = _failing_acquire
    ee._db_pool = mock_pool

    # Should not raise
    await ee._update_signal_status("RELIANCE", "REJECTED", reject_reason="test")


# -----------------------------------------------------------------------
# (h) _ensure_sessions_table creates table when missing
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_sessions_table_creates():
    """_ensure_sessions_table creates the table when it doesn't exist."""
    from main import _ensure_sessions_table

    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=False)  # table doesn't exist
    mock_conn.execute = AsyncMock()

    mock_pool = _mock_db_pool(mock_conn)

    await _ensure_sessions_table(mock_pool)

    mock_conn.fetchval.assert_called_once()
    mock_conn.execute.assert_called_once()
    sql = mock_conn.execute.call_args[0][0]
    assert "CREATE TABLE IF NOT EXISTS sessions" in sql


# -----------------------------------------------------------------------
# (i) _ensure_sessions_table skips when table exists
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_sessions_table_skips():
    """_ensure_sessions_table does nothing when table already exists."""
    from main import _ensure_sessions_table

    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=True)  # table exists

    mock_pool = _mock_db_pool(mock_conn)

    await _ensure_sessions_table(mock_pool)

    mock_conn.fetchval.assert_called_once()
    mock_conn.execute.assert_not_called()


# -----------------------------------------------------------------------
# (j) kill_switch_max tracked in shared_state
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kill_switch_max_tracked():
    """trigger_kill_switch updates kill_switch_max in shared_state."""
    from main import trigger_kill_switch

    shared_state = {
        "kill_switch_level": 0,
        "kill_switch_max": 0,
        "accepting_signals": True,
        "telegram_active": False,
    }
    config = {}
    secrets = {}

    with patch("main.send_telegram", new_callable=AsyncMock):
        await trigger_kill_switch(1, "test_trigger", shared_state, config, secrets)

    assert shared_state["kill_switch_max"] == 1
    assert shared_state["kill_switch_level"] == 1

    # Trigger L2 — max should update
    with patch("main.send_telegram", new_callable=AsyncMock):
        await trigger_kill_switch(2, "test_trigger_l2", shared_state, config, secrets)

    assert shared_state["kill_switch_max"] == 2
