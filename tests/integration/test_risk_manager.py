"""
TradeOS D8 Layer 2 — Risk Manager integration tests.

Requires TimescaleDB running with schema applied.
Set TRADEOS_TEST_DB_DSN to enable:
    export TRADEOS_TEST_DB_DSN="postgresql://user:pass@localhost/tradeos_test"

Tests are skipped automatically when the env var is unset.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal

import pytest
import pytz

IST = pytz.timezone("Asia/Kolkata")
DB_DSN = os.environ.get("TRADEOS_TEST_DB_DSN", "")

pytestmark = pytest.mark.skipif(
    not DB_DSN,
    reason="TRADEOS_TEST_DB_DSN not set — skipping integration tests",
)


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture
def session_date() -> date:
    return datetime.now(IST).date()


@pytest.fixture
async def db_pool(session_date: date):
    """Create asyncpg pool; close after test."""
    import asyncpg
    pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=4)
    yield pool
    await pool.close()


@pytest.fixture
def config() -> dict:
    return {
        "system": {"mode": "paper"},
        "capital": {"total": 500000},
        "risk": {"max_loss_per_trade_pct": 0.015},
    }


@pytest.fixture
def shared_state() -> dict:
    return {
        "open_positions": {},
        "daily_pnl_pct": 0.0,
        "daily_pnl_rs": 0.0,
        "consecutive_losses": 0,
        "kill_switch_level": 0,
    }


# ------------------------------------------------------------------
# test_full_trade_lifecycle
# ------------------------------------------------------------------

async def test_full_trade_lifecycle(db_pool, config, shared_state, session_date):
    """
    Full flow: size_position → on_fill → on_close.
    Verify:
      - trades table has one row
      - shared_state updated correctly throughout
    """
    from core.risk_manager import RiskManager

    async with RiskManager(config=config, shared_state=shared_state, db_pool=db_pool) as rm:
        # Simulate a signal
        class FakeSignal:
            theoretical_entry = Decimal("2500")
            stop_loss = Decimal("2450")

        qty = rm.size_position(FakeSignal())
        assert qty is not None and qty > 0

        # Fill
        await rm.on_fill(
            symbol="RELIANCE",
            direction="LONG",
            qty=qty,
            fill_price=Decimal("2500"),
            order_id="ORD-TEST-001",
            signal_id=1,
        )
        assert "RELIANCE" in shared_state["open_positions"]

        # Close
        await rm.on_close(
            symbol="RELIANCE",
            exit_price=Decimal("2600"),
            exit_reason="TARGET_HIT",
            exit_order_id="ORD-TEST-002",
        )
        assert "RELIANCE" not in shared_state["open_positions"]
        assert shared_state["daily_pnl_pct"] > 0.0

    # Verify DB row
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM trades WHERE session_date = $1 AND symbol = 'RELIANCE'"
            " AND entry_order_id = 'ORD-TEST-001'",
            session_date,
        )

    assert row is not None
    assert row["direction"] == "LONG"
    assert row["gross_pnl"] > 0
    assert row["net_pnl"] < row["gross_pnl"]  # charges deducted

    # Cleanup
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM trades WHERE session_date = $1 AND symbol = 'RELIANCE'"
            " AND entry_order_id = 'ORD-TEST-001'",
            session_date,
        )


# ------------------------------------------------------------------
# test_two_consecutive_losses_then_win
# ------------------------------------------------------------------

async def test_two_consecutive_losses_then_win(db_pool, config, shared_state, session_date):
    """
    Two losses → consecutive_losses = 2
    One win   → consecutive_losses = 0
    """
    from core.risk_manager import RiskManager

    async with RiskManager(config=config, shared_state=shared_state, db_pool=db_pool) as rm:
        for i, (symbol, entry, exit_price, reason) in enumerate([
            ("RELIANCE", Decimal("2500"), Decimal("2450"), "STOP_HIT"),   # loss
            ("INFY",     Decimal("1800"), Decimal("1750"), "STOP_HIT"),   # loss
            ("TCS",      Decimal("3500"), Decimal("3600"), "TARGET_HIT"), # win
        ], start=1):
            await rm.on_fill(
                symbol=symbol, direction="LONG", qty=10,
                fill_price=entry, order_id=f"ORD-{i}A", signal_id=i,
            )
            await rm.on_close(
                symbol=symbol, exit_price=exit_price,
                exit_reason=reason, exit_order_id=f"ORD-{i}B",
            )
            if i == 2:
                # After 2 losses
                assert shared_state["consecutive_losses"] == 2
        # After win
        assert shared_state["consecutive_losses"] == 0

    # Cleanup
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM trades WHERE session_date = $1 AND symbol IN ('RELIANCE','INFY','TCS')",
            session_date,
        )


# ------------------------------------------------------------------
# test_daily_loss_warning_event_written
# ------------------------------------------------------------------

async def test_daily_loss_warning_event_written(db_pool, config, shared_state, session_date):
    """
    Force daily_pnl_pct below -2% threshold.
    Verify system_events table has a WARNING DAILY_LOSS_WARNING row.
    """
    from core.risk_manager import RiskManager

    # Use a large position size to force big losses quickly
    # capital=500000, entry=2500, stop=2450, qty=80 (capped at 40%)
    # Then exit at 2000 → gross_loss = (2000-2500)*80 = -40000 → daily_pnl_pct ≈ -8%
    async with RiskManager(config=config, shared_state=shared_state, db_pool=db_pool) as rm:
        await rm.on_fill(
            symbol="RELIANCE", direction="LONG", qty=80,
            fill_price=Decimal("2500"), order_id="ORD-WARN-A", signal_id=99,
        )
        await rm.on_close(
            symbol="RELIANCE",
            exit_price=Decimal("2000"),  # big loss
            exit_reason="STOP_HIT",
            exit_order_id="ORD-WARN-B",
        )

        # daily_pnl_pct should be well below -0.02
        assert shared_state["daily_pnl_pct"] < -0.02

    # Verify system_event
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT * FROM system_events
               WHERE session_date = $1 AND event_type = 'DAILY_LOSS_WARNING'
               ORDER BY id DESC LIMIT 1""",
            session_date,
        )

    assert row is not None
    assert row["level"] == "WARNING"

    # Cleanup
    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM trades WHERE session_date = $1 AND symbol = 'RELIANCE'"
            " AND entry_order_id = 'ORD-WARN-A'",
            session_date,
        )
        await conn.execute(
            "DELETE FROM system_events WHERE session_date = $1 AND event_type = 'DAILY_LOSS_WARNING'",
            session_date,
        )
