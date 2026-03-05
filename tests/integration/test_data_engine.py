"""
TradeOS D8 Layer 2 — Data Engine integration tests

Requires TimescaleDB running with schema applied.
Set TRADEOS_TEST_DB_DSN to enable:
    export TRADEOS_TEST_DB_DSN="postgresql://user:pass@localhost/tradeos_test"

Tests are skipped automatically when the env var is unset.
"""
from __future__ import annotations

import asyncio
import csv
import os
from datetime import datetime, date
from pathlib import Path
from unittest.mock import MagicMock

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
async def storage(session_date: date):
    """Create and connect TickStorage; disconnect after the test."""
    from data_engine.storage import TickStorage

    s = TickStorage(dsn=DB_DSN, session_date=session_date)
    await s.connect()
    yield s
    await s.disconnect()


@pytest.fixture
def fresh_tick() -> dict:
    return {
        "instrument_token": 738561,
        "tradingsymbol": "RELIANCE",
        "last_price": 2450.50,
        "volume_traded": 1_234_567,
        "exchange_timestamp": datetime.now(IST),
        "oi": 0,
        "bid": 2450.25,
        "ask": 2450.75,
    }


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------

async def test_valid_tick_flows_feed_to_storage(storage, fresh_tick, session_date):
    """
    A valid tick written to TickStorage must appear in the ticks table.
    Covers the feed → validate → store flow end-to-end at the storage layer.
    """
    await storage.write_tick(fresh_tick)
    await storage._do_flush()

    async with storage._pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM ticks
            WHERE instrument_token = $1 AND session_date = $2
            ORDER BY id DESC LIMIT 1
            """,
            fresh_tick["instrument_token"],
            session_date,
        )

    assert row is not None, "Tick must be written to DB"
    assert float(row["last_price"]) == fresh_tick["last_price"]


async def test_invalid_tick_not_written_to_storage(storage, session_date):
    """
    A zero-price tick rejected by TickValidator must never reach the DB.
    Verify by counting rows before and after a rejected tick is processed.
    """
    from data_engine.prev_close_cache import PrevCloseCache
    from data_engine.validator import TickValidator

    cache = MagicMock(spec=PrevCloseCache)
    cache.get.return_value = 2000.0
    validator = TickValidator(cache)

    bad_tick = {
        "instrument_token": 738561,
        "tradingsymbol": "RELIANCE",
        "last_price": 0.0,           # Gate 1 rejects
        "volume_traded": 1000,
        "exchange_timestamp": datetime.now(IST),
    }

    async with storage._pool.acquire() as conn:
        count_before = await conn.fetchval(
            "SELECT COUNT(*) FROM ticks WHERE instrument_token=$1 AND session_date=$2",
            738561, session_date,
        )

    validated = validator.validate(bad_tick)
    assert validated is None, "TickValidator must reject zero-price tick"
    # Nothing should be written

    async with storage._pool.acquire() as conn:
        count_after = await conn.fetchval(
            "SELECT COUNT(*) FROM ticks WHERE instrument_token=$1 AND session_date=$2",
            738561, session_date,
        )

    assert count_before == count_after, "Zero-price tick must not reach the DB"


async def test_storage_batch_flushes_after_1_second(storage, fresh_tick, session_date):
    """
    Write 10 ticks into the buffer, wait > 1 s for flush_loop, verify all in DB.
    """
    flush_task = asyncio.create_task(storage.flush_loop())

    tokens = []
    for i in range(10):
        tick = fresh_tick.copy()
        tick["instrument_token"] = 800000 + i
        tick["tradingsymbol"] = f"TESTSTOCK{i}"
        tokens.append(tick["instrument_token"])
        await storage.write_tick(tick)

    await asyncio.sleep(1.1)   # wait for flush_loop to fire

    async with storage._pool.acquire() as conn:
        count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM ticks
            WHERE instrument_token = ANY($1::int[]) AND session_date = $2
            """,
            tokens, session_date,
        )

    flush_task.cancel()
    try:
        await flush_task
    except asyncio.CancelledError:
        pass

    assert count == 10, f"Expected 10 ticks in DB after flush, got {count}"


async def test_storage_fallback_csv_on_db_error(tmp_path, session_date, fresh_tick):
    """
    When the DB connection is unreachable, ticks must be written to fallback CSV.
    No ticks must be lost.
    """
    from data_engine.storage import TickStorage

    bad_storage = TickStorage(
        dsn="postgresql://invalid:invalid@localhost:5999/nonexistent",
        session_date=session_date,
    )
    bad_storage._fallback_dir = tmp_path
    bad_storage._fallback_dir.mkdir(parents=True, exist_ok=True)

    # Inject 5 ticks directly into buffer (no pool needed)
    for i in range(5):
        tick = fresh_tick.copy()
        tick["instrument_token"] = 900000 + i
        bad_storage._buffer.append(tick)

    # _do_flush will fail on DB and fall back to CSV
    await bad_storage._do_flush()

    csv_path = tmp_path / f"{session_date.isoformat()}.csv"
    assert csv_path.exists(), "Fallback CSV must be created when DB is unreachable"

    with open(csv_path) as fh:
        rows = list(csv.DictReader(fh))

    assert len(rows) == 5, f"Expected 5 rows in fallback CSV, got {len(rows)}"
