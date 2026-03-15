"""
TradeOS D8 Layer 1 — PrevCloseCache unit tests (4 mandatory cases)
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.data_engine.prev_close_cache import PrevCloseCache

INSTRUMENTS = [
    {"instrument_token": 738561, "tradingsymbol": "RELIANCE"},
    {"instrument_token": 408065, "tradingsymbol": "INFY"},
]

MOCK_OHLC = [
    {
        "date": "2026-03-04",
        "open": 2440.0,
        "high": 2460.0,
        "low": 2430.0,
        "close": 2450.0,
        "volume": 1_234_567,
    }
]


async def test_cache_loads_all_instruments():
    """
    load() fetches OHLC for every instrument and stores the close price.
    is_loaded() must be True after load() completes.
    """
    kite = MagicMock()
    kite.historical_data.return_value = MOCK_OHLC

    cache = PrevCloseCache(kite, INSTRUMENTS)
    await cache.load()

    assert cache.is_loaded()
    assert cache.get(738561) == 2450.0
    assert cache.get(408065) == 2450.0


async def test_cache_returns_none_for_unknown_token():
    """get() for a token not in the watchlist must return None, not raise."""
    kite = MagicMock()
    kite.historical_data.return_value = MOCK_OHLC

    cache = PrevCloseCache(kite, INSTRUMENTS)
    await cache.load()

    result = cache.get(999999)
    assert result is None


async def test_gate2_passes_when_prev_close_none():
    """
    When historical_data returns empty list, cache stores None for that token.
    Gate 2 rule: None must be returned so the caller (TickValidator) can PASS.
    """
    kite = MagicMock()
    kite.historical_data.return_value = []   # No data

    cache = PrevCloseCache(kite, [INSTRUMENTS[0]])
    await cache.load()

    assert cache.is_loaded()
    result = cache.get(738561)
    assert result is None, (
        "Empty historical data must store None so Gate 2 can PASS (D5 rule)"
    )


async def test_cache_partial_failure_does_not_block_startup():
    """
    If one instrument's API call fails, cache must still be marked loaded.
    The failed instrument stores None; the successful one stores its close.
    Startup must never be blocked by a single instrument failure.
    """
    kite = MagicMock()
    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("API timeout")   # first instrument fails
        return MOCK_OHLC                         # second succeeds

    kite.historical_data.side_effect = side_effect

    cache = PrevCloseCache(kite, INSTRUMENTS)
    # Must not raise
    await cache.load()

    assert cache.is_loaded(), "Cache must be loaded even after partial failure"
    assert cache.get(738561) is None,   "Failed instrument must be None"
    assert cache.get(408065) == 2450.0, "Successful instrument must have close price"
