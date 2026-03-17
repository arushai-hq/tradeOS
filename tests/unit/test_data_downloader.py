"""
Data Downloader — Unit tests.

Tests:
  (a) _chunk_date_range splits correctly for 5min (100-day limit)
  (b) _chunk_date_range splits correctly for 15min (200-day limit)
  (c) _chunk_date_range single chunk when within limit
  (d) _chunk_date_range empty when start > end
  (e) INTERVAL_MAP covers all intervals with correct KiteConnect strings
  (f) _insert_candles SQL contains ON CONFLICT DO NOTHING
  (g) Resume: _get_metadata returns row from DB
  (h) Resume: skip when metadata covers full range
  (i) --all flag includes all intervals and INDEX_INSTRUMENTS
  (j) Rate limiting: sleep called between chunks
  (k) Single symbol failure doesn't abort batch
  (l) _download_symbol collects candles from multiple chunks
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
    """365 days with 100-day limit = 4 chunks."""
    from tools.data_downloader import _chunk_date_range

    start = date(2025, 3, 17)
    end = date(2026, 3, 17)
    chunks = _chunk_date_range(start, end, "5min")

    assert len(chunks) == 4
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
    """1095 days with 200-day limit = 6 chunks."""
    from tools.data_downloader import _chunk_date_range

    start = date(2023, 3, 17)
    end = date(2026, 3, 17)
    chunks = _chunk_date_range(start, end, "15min")

    assert len(chunks) == 6
    assert chunks[0][0] == start
    assert chunks[-1][1] == end
    for c_start, c_end in chunks:
        assert (c_end - c_start).days < 200


def test_chunk_date_range_within_limit_single_chunk():
    """30 days with 200-day limit = 1 chunk."""
    from tools.data_downloader import _chunk_date_range

    start = date(2026, 2, 15)
    end = date(2026, 3, 17)
    chunks = _chunk_date_range(start, end, "15min")

    assert len(chunks) == 1
    assert chunks[0] == (start, end)


def test_chunk_date_range_start_after_end():
    """Empty when start > end."""
    from tools.data_downloader import _chunk_date_range

    chunks = _chunk_date_range(date(2026, 4, 1), date(2026, 3, 1), "day")
    assert chunks == []


# ---------------------------------------------------------------------------
# (e) Interval map completeness
# ---------------------------------------------------------------------------

def test_interval_map_covers_all():
    """All 5 intervals present with correct KiteConnect names."""
    from tools.data_downloader import (
        DEFAULT_DAYS,
        INTERVAL_MAP,
        INTERVAL_MAX_DAYS,
    )

    expected = {"5min", "15min", "30min", "1hour", "day"}
    assert set(INTERVAL_MAP.keys()) == expected
    assert set(INTERVAL_MAX_DAYS.keys()) == expected
    assert set(DEFAULT_DAYS.keys()) == expected

    # KiteConnect API names
    assert INTERVAL_MAP["5min"] == "5minute"
    assert INTERVAL_MAP["15min"] == "15minute"
    assert INTERVAL_MAP["30min"] == "30minute"
    assert INTERVAL_MAP["1hour"] == "60minute"
    assert INTERVAL_MAP["day"] == "day"


# ---------------------------------------------------------------------------
# (f) ON CONFLICT in insert SQL
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insert_candles_on_conflict():
    """INSERT statement uses ON CONFLICT DO NOTHING."""
    from tools.data_downloader import _insert_candles

    mock_conn = AsyncMock()
    mock_conn.executemany = AsyncMock(return_value=None)
    mock_pool = _mock_pool(mock_conn)

    candles = [
        {"date": datetime(2026, 3, 1, 9, 15), "open": 100, "high": 105,
         "low": 98, "close": 103, "volume": 50000},
    ]

    await _insert_candles(mock_pool, 738561, "RELIANCE", "15min", candles)
    mock_conn.executemany.assert_called_once()
    sql = mock_conn.executemany.call_args[0][0]
    assert "ON CONFLICT" in sql
    assert "DO NOTHING" in sql


# ---------------------------------------------------------------------------
# (g) Resume — _get_metadata
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_metadata_returns_row():
    """_get_metadata returns dict when metadata exists."""
    from tools.data_downloader import _get_metadata

    mock_row = {
        "date_from": date(2023, 3, 17),
        "date_to": date(2026, 3, 17),
        "rows_downloaded": 15000,
    }
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=mock_row)
    mock_pool = _mock_pool(mock_conn)

    result = await _get_metadata(mock_pool, 738561, "15min")
    assert result is not None
    assert result["date_to"] == date(2026, 3, 17)


# ---------------------------------------------------------------------------
# (h) Resume — skip when fully downloaded
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_skip_when_fully_downloaded():
    """_download_and_store skips symbol when metadata covers full range."""
    from tools.data_downloader import _download_and_store

    mock_kite = MagicMock()

    # Mock pool where _get_metadata returns covering range
    mock_conn = AsyncMock()
    today = date.today()
    start = today - timedelta(days=30)
    mock_conn.fetchrow = AsyncMock(return_value={
        "date_from": start - timedelta(days=10),
        "date_to": today + timedelta(days=1),
        "rows_downloaded": 5000,
    })
    mock_pool = _mock_pool(mock_conn)

    instruments = [{"symbol": "RELIANCE", "token": 738561}]
    result = await _download_and_store(mock_kite, mock_pool, instruments, "15min", 30)

    assert result["skipped"] == 1
    assert result["downloaded"] == 0
    # KiteConnect should NOT have been called
    mock_kite.historical_data.assert_not_called()


# ---------------------------------------------------------------------------
# (i) --all includes all intervals + INDEX_INSTRUMENTS
# ---------------------------------------------------------------------------

def test_all_flag_constants():
    """DEFAULT_DAYS and INDEX_INSTRUMENTS have expected values."""
    from tools.data_downloader import DEFAULT_DAYS, INDEX_INSTRUMENTS

    assert DEFAULT_DAYS["5min"] == 365
    assert DEFAULT_DAYS["15min"] == 1095
    assert DEFAULT_DAYS["day"] == 2555

    symbols = [i["symbol"] for i in INDEX_INSTRUMENTS]
    assert "NIFTY 50" in symbols
    assert "INDIA VIX" in symbols
    assert INDEX_INSTRUMENTS[0]["token"] == 256265
    assert INDEX_INSTRUMENTS[1]["token"] == 264969


# ---------------------------------------------------------------------------
# (j) Rate limiting between chunks
# ---------------------------------------------------------------------------

def test_rate_limiting_between_api_calls():
    """time.sleep called with RATE_LIMIT_SECS between chunk API calls."""
    from tools.data_downloader import RATE_LIMIT_SECS, _download_symbol

    mock_kite = MagicMock()
    mock_kite.historical_data.return_value = [
        {"date": datetime(2026, 3, 1, 9, 15), "open": 100, "high": 105,
         "low": 98, "close": 103, "volume": 50000},
    ]

    with patch("tools.data_downloader.time.sleep") as mock_sleep:
        # 250 days with 200-day limit = 2 chunks → 1 sleep
        result = _download_symbol(
            mock_kite, 738561, "RELIANCE", "15min",
            date(2025, 7, 1), date(2026, 3, 17),
        )

    assert mock_kite.historical_data.call_count == 2
    assert mock_sleep.call_count == 1
    mock_sleep.assert_called_with(RATE_LIMIT_SECS)
    assert len(result) == 2  # 1 candle per chunk


# ---------------------------------------------------------------------------
# (k) Single failure doesn't abort batch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_failure_continues():
    """One symbol raising doesn't stop other downloads."""
    from tools.data_downloader import _download_and_store

    mock_kite = MagicMock()
    # First call fails, second succeeds
    mock_kite.historical_data.side_effect = [
        Exception("API timeout"),
        [{"date": datetime(2026, 3, 1, 9, 15), "open": 100, "high": 105,
          "low": 98, "close": 103, "volume": 50000}],
    ]

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)  # No prior metadata
    mock_conn.executemany = AsyncMock(return_value=None)
    mock_conn.execute = AsyncMock(return_value=None)
    mock_pool = _mock_pool(mock_conn)

    instruments = [
        {"symbol": "INFY", "token": 408065},
        {"symbol": "RELIANCE", "token": 738561},
    ]

    with patch("tools.data_downloader.time.sleep"):
        result = await _download_and_store(mock_kite, mock_pool, instruments, "day", 30)

    assert len(result["failed"]) == 1
    assert result["failed"][0]["symbol"] == "INFY"
    assert result["downloaded"] == 1


# ---------------------------------------------------------------------------
# (l) _download_symbol collects from multiple chunks
# ---------------------------------------------------------------------------

def test_download_symbol_collects_all_chunks():
    """_download_symbol concatenates candles from all chunks."""
    from tools.data_downloader import _download_symbol

    mock_kite = MagicMock()
    # 2 chunks → 2 API calls, each returning 3 candles
    mock_kite.historical_data.side_effect = [
        [{"date": datetime(2025, 7, 1 + i), "open": 100, "high": 105,
          "low": 98, "close": 103, "volume": 50000} for i in range(3)],
        [{"date": datetime(2026, 1, 1 + i), "open": 200, "high": 205,
          "low": 198, "close": 203, "volume": 60000} for i in range(3)],
    ]

    with patch("tools.data_downloader.time.sleep"):
        result = _download_symbol(
            mock_kite, 738561, "RELIANCE", "15min",
            date(2025, 7, 1), date(2026, 3, 17),
        )

    assert len(result) == 6
    assert mock_kite.historical_data.call_count == 2
