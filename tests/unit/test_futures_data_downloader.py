"""
Futures Data Downloader — Unit tests.

Tests:
  (a) _chunk_date_range splits correctly for 5min (100-day limit)
  (b) _chunk_date_range splits correctly for 15min (200-day limit)
  (c) _chunk_date_range single chunk when within limit
  (d) _chunk_date_range empty when start > end
  (e) INTERVAL_MAP covers all futures intervals with correct KiteConnect strings
  (f) _insert_candles SQL contains ON CONFLICT DO NOTHING + tradingsymbol/expiry
  (g) Resume: _get_metadata returns row from DB
  (h) Resume: skip when metadata covers full range (day/continuous only)
  (i) Rate limiting: sleep called between chunks
  (j) Single instrument failure doesn't abort batch
  (k1) _download_futures day uses continuous=True
  (k2) _download_futures intraday uses continuous=False (default)
  (l) _resolve_futures_contracts returns ALL active contracts
  (m) _resolve_futures_contracts excludes unwanted instruments
  (n) _resolve_futures_contracts skips expired contracts
  (o) _pick_nearest_per_instrument picks one per name
  (p) _download_and_store day mode uses continuous=True
  (q) _download_and_store intraday downloads all contracts separately
  (r) _ensure_tables migrates old schema (DROP + RECREATE)
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_pool(mock_conn):
    """Create a mock asyncpg pool where acquire() returns an async CM."""
    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire.return_value = mock_cm
    return pool


# ---------------------------------------------------------------------------
# (a-d) Date chunking — pure logic
# ---------------------------------------------------------------------------

def test_chunk_date_range_5min_splits_at_100_days():
    """548 days with 100-day limit = 6 chunks."""
    from tools.futures_data_downloader import _chunk_date_range

    start = date(2024, 9, 17)
    end = date(2026, 3, 17)
    chunks = _chunk_date_range(start, end, "5min")

    assert len(chunks) == 6
    # First chunk starts at start
    assert chunks[0][0] == start
    # Last chunk ends at end
    assert chunks[-1][1] == end
    # Each chunk ≤ 100 days
    for c_start, c_end in chunks:
        assert (c_end - c_start).days < 100
    # Chunks are contiguous
    for i in range(len(chunks) - 1):
        assert chunks[i + 1][0] == chunks[i][1] + timedelta(days=1)


def test_chunk_date_range_15min_splits_at_200_days():
    """548 days with 200-day limit = 3 chunks."""
    from tools.futures_data_downloader import _chunk_date_range

    start = date(2024, 9, 17)
    end = date(2026, 3, 17)
    chunks = _chunk_date_range(start, end, "15min")

    assert len(chunks) == 3
    assert chunks[0][0] == start
    assert chunks[-1][1] == end
    for c_start, c_end in chunks:
        assert (c_end - c_start).days < 200


def test_chunk_date_range_within_limit_single_chunk():
    """30 days with 200-day limit = 1 chunk."""
    from tools.futures_data_downloader import _chunk_date_range

    start = date(2026, 2, 15)
    end = date(2026, 3, 17)
    chunks = _chunk_date_range(start, end, "15min")

    assert len(chunks) == 1
    assert chunks[0] == (start, end)


def test_chunk_date_range_start_after_end():
    """Empty when start > end."""
    from tools.futures_data_downloader import _chunk_date_range

    chunks = _chunk_date_range(date(2026, 4, 1), date(2026, 3, 1), "day")
    assert chunks == []


# ---------------------------------------------------------------------------
# (e) Interval map completeness
# ---------------------------------------------------------------------------

def test_interval_map_covers_futures_intervals():
    """All 3 futures intervals present with correct KiteConnect names."""
    from tools.futures_data_downloader import (
        DEFAULT_DAYS,
        INTERVAL_MAP,
        INTERVAL_MAX_DAYS,
    )

    expected = {"5min", "15min", "day"}
    assert set(INTERVAL_MAP.keys()) == expected
    assert set(INTERVAL_MAX_DAYS.keys()) == expected
    assert set(DEFAULT_DAYS.keys()) == expected

    # KiteConnect API names
    assert INTERVAL_MAP["5min"] == "5minute"
    assert INTERVAL_MAP["15min"] == "15minute"
    assert INTERVAL_MAP["day"] == "day"

    # Chunk limits
    assert INTERVAL_MAX_DAYS["5min"] == 100
    assert INTERVAL_MAX_DAYS["15min"] == 200
    assert INTERVAL_MAX_DAYS["day"] == 2000


# ---------------------------------------------------------------------------
# (f) ON CONFLICT in insert SQL — now with tradingsymbol + expiry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insert_candles_on_conflict():
    """INSERT statement uses ON CONFLICT DO NOTHING and includes tradingsymbol/expiry."""
    from tools.futures_data_downloader import _insert_candles

    mock_conn = AsyncMock()
    mock_conn.executemany = AsyncMock(return_value=None)
    mock_pool = _mock_pool(mock_conn)

    candles = [
        {"date": datetime(2026, 3, 1, 9, 15), "open": 22000, "high": 22100,
         "low": 21900, "close": 22050, "volume": 500000, "oi": 12000000},
    ]

    await _insert_candles(
        mock_pool, "NIFTY", "15min", candles,
        tradingsymbol="NIFTY26MARFUT", expiry=date(2026, 3, 27),
    )
    mock_conn.executemany.assert_called_once()
    sql = mock_conn.executemany.call_args[0][0]
    assert "ON CONFLICT" in sql
    assert "DO NOTHING" in sql
    assert "tradingsymbol" in sql
    assert "expiry" in sql
    # 11 params: instrument, tradingsymbol, expiry, interval, timestamp, OHLCV, oi
    assert "$11" in sql

    # Verify the row tuple includes tradingsymbol and expiry
    rows = mock_conn.executemany.call_args[0][1]
    assert rows[0][1] == "NIFTY26MARFUT"  # tradingsymbol
    assert rows[0][2] == date(2026, 3, 27)  # expiry


# ---------------------------------------------------------------------------
# (g) Resume — _get_metadata
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_metadata_returns_row():
    """_get_metadata returns dict when metadata exists."""
    from tools.futures_data_downloader import _get_metadata

    mock_row = {
        "first_candle": datetime(2024, 9, 17, 9, 15),
        "last_candle": datetime(2026, 3, 17, 15, 0),
        "candle_count": 50000,
    }
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=mock_row)
    mock_pool = _mock_pool(mock_conn)

    result = await _get_metadata(mock_pool, "NIFTY", "15min")
    assert result is not None
    assert result["candle_count"] == 50000


# ---------------------------------------------------------------------------
# (h) Resume — skip when fully downloaded (day/continuous only)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_skip_when_fully_downloaded():
    """_download_and_store skips instrument when metadata covers full range (day mode)."""
    from tools.futures_data_downloader import _download_and_store

    mock_kite = MagicMock()

    # Mock pool where _get_metadata returns covering range
    mock_conn = AsyncMock()
    today = date.today()
    start = today - timedelta(days=30)
    mock_conn.fetchrow = AsyncMock(return_value={
        "first_candle": datetime(
            start.year, start.month, start.day, 9, 15,
        ) - timedelta(days=10),
        "last_candle": datetime(
            today.year, today.month, today.day, 15, 0,
        ) + timedelta(days=1),
        "candle_count": 50000,
    })
    mock_pool = _mock_pool(mock_conn)

    instruments = [
        {"name": "NIFTY", "token": 256265, "lot_size": 65,
         "expiry": today + timedelta(days=10), "tradingsymbol": "NIFTY26MARFUT"},
    ]
    # day interval uses continuous mode — skip logic applies
    result = await _download_and_store(mock_kite, mock_pool, instruments, "day", 30)

    assert result["skipped"] == 1
    assert result["downloaded"] == 0
    # KiteConnect should NOT have been called
    mock_kite.historical_data.assert_not_called()


# ---------------------------------------------------------------------------
# (i) Rate limiting between chunks
# ---------------------------------------------------------------------------

def test_rate_limiting_between_api_calls():
    """time.sleep called with RATE_LIMIT_SECS between chunk API calls."""
    from tools.futures_data_downloader import RATE_LIMIT_SECS, _download_futures

    mock_kite = MagicMock()
    mock_kite.historical_data.return_value = [
        {"date": datetime(2026, 3, 1, 9, 15), "open": 22000, "high": 22100,
         "low": 21900, "close": 22050, "volume": 500000, "oi": 12000000},
    ]

    with patch("tools.futures_data_downloader.time.sleep") as mock_sleep:
        # 250 days with 200-day limit = 2 chunks → 1 sleep
        result = _download_futures(
            mock_kite, 256265, "NIFTY", "15min",
            date(2025, 7, 1), date(2026, 3, 17),
        )

    assert mock_kite.historical_data.call_count == 2
    assert mock_sleep.call_count == 1
    mock_sleep.assert_called_with(RATE_LIMIT_SECS)
    assert len(result) == 2  # 1 candle per chunk


# ---------------------------------------------------------------------------
# (j) Single failure doesn't abort batch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_failure_continues():
    """One instrument raising doesn't stop other downloads."""
    from tools.futures_data_downloader import _download_and_store

    mock_kite = MagicMock()
    # First call fails, second succeeds
    mock_kite.historical_data.side_effect = [
        Exception("API timeout"),
        [{"date": datetime(2026, 3, 1, 9, 15), "open": 22000, "high": 22100,
          "low": 21900, "close": 22050, "volume": 500000, "oi": 12000000}],
    ]

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)  # No prior metadata
    mock_conn.executemany = AsyncMock(return_value=None)
    mock_conn.execute = AsyncMock(return_value=None)
    mock_pool = _mock_pool(mock_conn)

    instruments = [
        {"name": "NIFTY", "token": 256265, "lot_size": 65,
         "expiry": date.today() + timedelta(days=10), "tradingsymbol": "NIFTY26MARFUT"},
        {"name": "BANKNIFTY", "token": 260105, "lot_size": 30,
         "expiry": date.today() + timedelta(days=10), "tradingsymbol": "BANKNIFTY26MARFUT"},
    ]

    with patch("tools.futures_data_downloader.time.sleep"):
        result = await _download_and_store(mock_kite, mock_pool, instruments, "day", 30)

    assert len(result["failed"]) == 1
    assert result["failed"][0]["instrument"] == "NIFTY"
    assert result["downloaded"] == 1


# ---------------------------------------------------------------------------
# (k1) _download_futures day uses continuous=True
# ---------------------------------------------------------------------------

def test_download_futures_day_uses_continuous_true():
    """_download_futures passes continuous=True when called with continuous=True."""
    from tools.futures_data_downloader import _download_futures

    mock_kite = MagicMock()
    mock_kite.historical_data.return_value = [
        {"date": datetime(2026, 3, 1, 9, 15), "open": 22000, "high": 22100,
         "low": 21900, "close": 22050, "volume": 500000, "oi": 12000000},
    ]

    with patch("tools.futures_data_downloader.time.sleep"):
        _download_futures(
            mock_kite, 256265, "NIFTY", "day",
            date(2026, 3, 1), date(2026, 3, 15),
            continuous=True,
        )

    mock_kite.historical_data.assert_called_once()
    call_kwargs = mock_kite.historical_data.call_args
    assert call_kwargs.kwargs.get("continuous") is True
    assert call_kwargs.kwargs.get("oi") is True


# ---------------------------------------------------------------------------
# (k2) _download_futures intraday uses continuous=False (default)
# ---------------------------------------------------------------------------

def test_download_futures_intraday_uses_continuous_false():
    """_download_futures defaults to continuous=False for intraday."""
    from tools.futures_data_downloader import _download_futures

    mock_kite = MagicMock()
    mock_kite.historical_data.return_value = [
        {"date": datetime(2026, 3, 1, 9, 15), "open": 22000, "high": 22100,
         "low": 21900, "close": 22050, "volume": 500000, "oi": 12000000},
    ]

    with patch("tools.futures_data_downloader.time.sleep"):
        _download_futures(
            mock_kite, 256265, "NIFTY", "15min",
            date(2026, 3, 1), date(2026, 3, 15),
        )

    mock_kite.historical_data.assert_called_once()
    call_kwargs = mock_kite.historical_data.call_args
    assert call_kwargs.kwargs.get("continuous") is False
    assert call_kwargs.kwargs.get("oi") is True


# ---------------------------------------------------------------------------
# (l) _resolve_futures_contracts returns ALL active contracts
# ---------------------------------------------------------------------------

def test_resolve_futures_contracts_returns_all_active():
    """_resolve_futures_contracts returns ALL active contracts, not just nearest."""
    from tools.futures_data_downloader import _resolve_futures_contracts

    mock_kite = MagicMock()
    today = date.today()
    near_expiry = today + timedelta(days=10)
    far_expiry = today + timedelta(days=40)

    mock_kite.instruments.return_value = [
        {"instrument_token": 11111, "tradingsymbol": "NIFTY26MARFUT", "name": "NIFTY",
         "expiry": near_expiry, "lot_size": 65, "segment": "NFO-FUT", "exchange": "NFO"},
        {"instrument_token": 22222, "tradingsymbol": "NIFTY26APRFUT", "name": "NIFTY",
         "expiry": far_expiry, "lot_size": 65, "segment": "NFO-FUT", "exchange": "NFO"},
        {"instrument_token": 33333, "tradingsymbol": "BANKNIFTY26MARFUT", "name": "BANKNIFTY",
         "expiry": near_expiry, "lot_size": 30, "segment": "NFO-FUT", "exchange": "NFO"},
    ]

    config = {
        "futures": {
            "instruments": [
                {"name": "NIFTY", "lot_size": 65, "exclude_prefixes": []},
                {"name": "BANKNIFTY", "lot_size": 30, "exclude_prefixes": []},
            ],
        },
    }

    result = _resolve_futures_contracts(mock_kite, config)
    # Should return ALL 3 contracts (2 NIFTY + 1 BANKNIFTY)
    assert len(result) == 3

    nifty_contracts = [r for r in result if r["name"] == "NIFTY"]
    assert len(nifty_contracts) == 2  # Both near and far expiry
    assert nifty_contracts[0]["expiry"] < nifty_contracts[1]["expiry"]  # Sorted by expiry

    banknifty = [r for r in result if r["name"] == "BANKNIFTY"]
    assert len(banknifty) == 1
    assert banknifty[0]["token"] == 33333


# ---------------------------------------------------------------------------
# (m) _resolve_futures_contracts excludes unwanted instruments
# ---------------------------------------------------------------------------

def test_resolve_futures_contracts_excludes_unwanted():
    """_resolve_futures_contracts excludes FINNIFTY, MIDCPNIFTY, etc."""
    from tools.futures_data_downloader import _resolve_futures_contracts

    mock_kite = MagicMock()
    near_expiry = date.today() + timedelta(days=10)

    mock_kite.instruments.return_value = [
        {"instrument_token": 11111, "name": "NIFTY", "tradingsymbol": "NIFTY26MARFUT",
         "expiry": near_expiry, "lot_size": 65, "segment": "NFO-FUT", "exchange": "NFO"},
        {"instrument_token": 33333, "name": "FINNIFTY", "tradingsymbol": "FINNIFTY26MARFUT",
         "expiry": near_expiry, "lot_size": 25, "segment": "NFO-FUT", "exchange": "NFO"},
        {"instrument_token": 44444, "name": "MIDCPNIFTY", "tradingsymbol": "MIDCPNIFTY26MARFUT",
         "expiry": near_expiry, "lot_size": 50, "segment": "NFO-FUT", "exchange": "NFO"},
    ]

    config = {
        "futures": {
            "instruments": [
                {"name": "NIFTY", "lot_size": 65,
                 "exclude_prefixes": ["FINNIFTY", "MIDCPNIFTY"]},
            ],
        },
    }

    result = _resolve_futures_contracts(mock_kite, config)
    names = [r["name"] for r in result]
    assert "NIFTY" in names
    assert "FINNIFTY" not in names
    assert "MIDCPNIFTY" not in names


# ---------------------------------------------------------------------------
# (n) _resolve_futures_contracts skips expired contracts
# ---------------------------------------------------------------------------

def test_resolve_futures_contracts_skips_expired():
    """_resolve_futures_contracts ignores contracts with past expiry dates."""
    from tools.futures_data_downloader import _resolve_futures_contracts

    mock_kite = MagicMock()
    today = date.today()
    expired = today - timedelta(days=5)
    active = today + timedelta(days=25)

    mock_kite.instruments.return_value = [
        {"instrument_token": 11111, "name": "NIFTY", "tradingsymbol": "NIFTY26FEBFUT",
         "expiry": expired, "lot_size": 65, "segment": "NFO-FUT", "exchange": "NFO"},
        {"instrument_token": 22222, "name": "NIFTY", "tradingsymbol": "NIFTY26MARFUT",
         "expiry": active, "lot_size": 65, "segment": "NFO-FUT", "exchange": "NFO"},
    ]

    config = {
        "futures": {
            "instruments": [
                {"name": "NIFTY", "lot_size": 65, "exclude_prefixes": []},
            ],
        },
    }

    result = _resolve_futures_contracts(mock_kite, config)
    assert len(result) == 1
    assert result[0]["token"] == 22222  # Active contract, not expired
    assert result[0]["expiry"] == active


# ---------------------------------------------------------------------------
# (o) _pick_nearest_per_instrument picks one per name
# ---------------------------------------------------------------------------

def test_pick_nearest_per_instrument():
    """_pick_nearest_per_instrument returns only nearest-expiry per instrument."""
    from tools.futures_data_downloader import _pick_nearest_per_instrument

    today = date.today()
    contracts = [
        {"name": "NIFTY", "token": 11111, "lot_size": 65,
         "expiry": today + timedelta(days=10), "tradingsymbol": "NIFTY26MARFUT"},
        {"name": "NIFTY", "token": 22222, "lot_size": 65,
         "expiry": today + timedelta(days=40), "tradingsymbol": "NIFTY26APRFUT"},
        {"name": "BANKNIFTY", "token": 33333, "lot_size": 30,
         "expiry": today + timedelta(days=10), "tradingsymbol": "BANKNIFTY26MARFUT"},
        {"name": "BANKNIFTY", "token": 44444, "lot_size": 30,
         "expiry": today + timedelta(days=40), "tradingsymbol": "BANKNIFTY26APRFUT"},
    ]

    result = _pick_nearest_per_instrument(contracts)
    assert len(result) == 2

    by_name = {r["name"]: r for r in result}
    assert by_name["NIFTY"]["token"] == 11111  # Nearest
    assert by_name["BANKNIFTY"]["token"] == 33333  # Nearest


# ---------------------------------------------------------------------------
# (p) _download_and_store day mode uses continuous=True
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_download_and_store_day_uses_continuous():
    """Day interval downloads with continuous=True via _pick_nearest_per_instrument."""
    from tools.futures_data_downloader import _download_and_store

    mock_kite = MagicMock()
    mock_kite.historical_data.return_value = [
        {"date": datetime(2026, 3, 1, 9, 15), "open": 22000, "high": 22100,
         "low": 21900, "close": 22050, "volume": 500000, "oi": 12000000},
    ]

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)  # No prior metadata
    mock_conn.executemany = AsyncMock(return_value=None)
    mock_conn.execute = AsyncMock(return_value=None)
    mock_pool = _mock_pool(mock_conn)

    today = date.today()
    contracts = [
        {"name": "NIFTY", "token": 11111, "lot_size": 65,
         "expiry": today + timedelta(days=10), "tradingsymbol": "NIFTY26MARFUT"},
        {"name": "NIFTY", "token": 22222, "lot_size": 65,
         "expiry": today + timedelta(days=40), "tradingsymbol": "NIFTY26APRFUT"},
    ]

    with patch("tools.futures_data_downloader.time.sleep"):
        result = await _download_and_store(mock_kite, mock_pool, contracts, "day", 30)

    # Should only download once (nearest contract per instrument)
    assert mock_kite.historical_data.call_count == 1
    call_kwargs = mock_kite.historical_data.call_args
    assert call_kwargs.kwargs.get("continuous") is True

    # Insert should use empty tradingsymbol for daily continuous
    insert_rows = mock_conn.executemany.call_args[0][1]
    assert insert_rows[0][1] == ""  # tradingsymbol = ""
    assert insert_rows[0][2] is None  # expiry = None


# ---------------------------------------------------------------------------
# (q) _download_and_store intraday downloads all contracts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_download_and_store_intraday_downloads_all_contracts():
    """Intraday interval downloads each contract separately with continuous=False."""
    from tools.futures_data_downloader import _download_and_store

    mock_kite = MagicMock()
    mock_kite.historical_data.return_value = [
        {"date": datetime(2026, 3, 1, 9, 15), "open": 22000, "high": 22100,
         "low": 21900, "close": 22050, "volume": 500000, "oi": 12000000},
    ]

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)  # No prior metadata
    mock_conn.executemany = AsyncMock(return_value=None)
    mock_conn.execute = AsyncMock(return_value=None)
    mock_pool = _mock_pool(mock_conn)

    today = date.today()
    near = today + timedelta(days=10)
    far = today + timedelta(days=40)

    contracts = [
        {"name": "NIFTY", "token": 11111, "lot_size": 65,
         "expiry": near, "tradingsymbol": "NIFTY26MARFUT"},
        {"name": "NIFTY", "token": 22222, "lot_size": 65,
         "expiry": far, "tradingsymbol": "NIFTY26APRFUT"},
    ]

    with patch("tools.futures_data_downloader.time.sleep"):
        result = await _download_and_store(mock_kite, mock_pool, contracts, "15min", 30)

    # Should download BOTH contracts (not just nearest)
    assert mock_kite.historical_data.call_count == 2
    # All calls should use continuous=False
    for call in mock_kite.historical_data.call_args_list:
        assert call.kwargs.get("continuous") is False

    assert result["downloaded"] == 2


# ---------------------------------------------------------------------------
# (r) _ensure_tables migrates old schema
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_tables_migrates_old_schema():
    """_ensure_tables drops and recreates tables when tradingsymbol column missing."""
    from tools.futures_data_downloader import _ensure_tables

    mock_conn = AsyncMock()
    # Table exists but tradingsymbol column does not
    mock_conn.fetchval = AsyncMock(side_effect=[True, False])
    mock_conn.execute = AsyncMock(return_value=None)
    mock_pool = _mock_pool(mock_conn)

    await _ensure_tables(mock_pool)

    # Should have called DROP on both tables + CREATE
    sql_calls = [mock_conn.execute.call_args_list[i][0][0] for i in range(len(mock_conn.execute.call_args_list))]
    assert any("DROP TABLE" in s and "backtest_futures_candles" in s for s in sql_calls)
    assert any("DROP TABLE" in s and "backtest_futures_metadata" in s for s in sql_calls)
    assert any("CREATE TABLE" in s for s in sql_calls)
