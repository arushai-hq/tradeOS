"""
TradeOS — pytest conftest.py

Shared fixtures for unit and integration tests.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest
import pytz

IST = pytz.timezone("Asia/Kolkata")


@pytest.fixture
def ist_now() -> datetime:
    """Current IST datetime (timezone-aware)."""
    return datetime.now(IST)


@pytest.fixture
def valid_tick(ist_now: datetime) -> dict:
    """
    Well-formed KiteConnect tick dict that should pass all 5 gates.

    price=2050 is within ±20% of prev_close=2000 used by mock_prev_close_cache.
    """
    return {
        "instrument_token": 738561,
        "tradingsymbol": "RELIANCE",
        "last_price": 2050.0,
        "volume_traded": 1_234_567,
        "exchange_timestamp": ist_now,
        "ohlc": {"open": 2040.0, "high": 2060.0, "low": 2035.0, "close": 2000.0},
        "oi": 0,
        "bid": 2049.75,
        "ask": 2050.25,
    }


@pytest.fixture
def mock_prev_close_cache() -> MagicMock:
    """Mock PrevCloseCache returning 2000.0 for all instrument tokens."""
    from core.data_engine.prev_close_cache import PrevCloseCache

    cache = MagicMock(spec=PrevCloseCache)
    cache.get.return_value = 2000.0
    cache.is_loaded.return_value = True
    return cache


@pytest.fixture
def mock_kite() -> MagicMock:
    """Minimal mock KiteConnect instance."""
    kite = MagicMock()
    kite.api_key = "test_api_key"
    kite.access_token = "test_access_token"
    return kite
