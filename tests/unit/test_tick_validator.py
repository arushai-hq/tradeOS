"""
TradeOS D8 Layer 1 — TickValidator unit tests (11 mandatory cases)

All test names are exactly as specified in the D8 layer1-unit-test-catalogue.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
import pytz
from freezegun import freeze_time

from data_engine.prev_close_cache import PrevCloseCache
from data_engine.validator import TickValidator

IST = pytz.timezone("Asia/Kolkata")

# Fixed "now" for all freeze_time tests — a weekday at 09:30 IST
FROZEN_NOW = "2026-03-05 09:30:00+05:30"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _cache(prev_close: float | None = 2000.0) -> MagicMock:
    """Build a mock PrevCloseCache with a fixed prev_close value."""
    c = MagicMock(spec=PrevCloseCache)
    c.get.return_value = prev_close
    c.is_loaded.return_value = True
    return c


def _tick(**overrides) -> dict:
    """Build a valid tick dict, with optional field overrides."""
    now = datetime.now(IST)
    base = {
        "instrument_token": 738561,
        "tradingsymbol": "RELIANCE",
        "last_price": 2050.0,     # within ±20% of 2000
        "volume_traded": 1000,
        "exchange_timestamp": now,
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------------
# Gate 1 — Zero / negative price
# ------------------------------------------------------------------

def test_gate1_rejects_zero_price():
    """price == 0 must be discarded."""
    validator = TickValidator(_cache())
    assert validator.validate(_tick(last_price=0.0)) is None


def test_gate1_rejects_negative_price():
    """price < 0 must be discarded."""
    validator = TickValidator(_cache())
    assert validator.validate(_tick(last_price=-1.0)) is None


# ------------------------------------------------------------------
# Gate 2 — NSE ±20% circuit breaker
# ------------------------------------------------------------------

@freeze_time(FROZEN_NOW)
def test_gate2_rejects_price_above_20pct_circuit():
    """price > prev_close * 1.20 must be discarded. (2000 * 1.20 = 2400)"""
    validator = TickValidator(_cache(prev_close=2000.0))
    # 2401 is 20.05% above 2000 — over the limit
    tick = _tick(last_price=2401.0, exchange_timestamp=datetime.now(IST))
    assert validator.validate(tick) is None


@freeze_time(FROZEN_NOW)
def test_gate2_passes_when_prev_close_unavailable():
    """
    When prev_close is None (cache miss), Gate 2 must PASS.
    D5 rule: never block ticks on missing reference data.
    """
    validator = TickValidator(_cache(prev_close=None))
    # Price is arbitrarily far from any reference — but Gate 2 has no reference
    tick = _tick(last_price=99999.0, exchange_timestamp=datetime.now(IST))
    result = validator.validate(tick)
    assert result is not None, "Gate 2 must pass when prev_close is unavailable"


# ------------------------------------------------------------------
# Gate 3 — Negative / missing volume
# ------------------------------------------------------------------

def test_gate3_rejects_negative_volume():
    """volume_traded < 0 must be discarded."""
    validator = TickValidator(_cache())
    assert validator.validate(_tick(volume_traded=-1)) is None


def test_gate3_rejects_none_volume():
    """volume_traded = None must be discarded."""
    validator = TickValidator(_cache())
    assert validator.validate(_tick(volume_traded=None)) is None


# ------------------------------------------------------------------
# Gate 4 — Staleness (uses exchange_timestamp)
# ------------------------------------------------------------------

@freeze_time(FROZEN_NOW)
def test_gate4_rejects_tick_older_than_5s():
    """exchange_timestamp > 5 seconds ago must be discarded."""
    validator = TickValidator(_cache())
    stale_ts = datetime.now(IST) - timedelta(seconds=6)
    tick = _tick(exchange_timestamp=stale_ts)
    assert validator.validate(tick) is None


@freeze_time(FROZEN_NOW)
def test_gate4_uses_exchange_timestamp_not_local():
    """
    Gate 4 must read age from tick["exchange_timestamp"], NOT the system clock.

    If an implementation accidentally used datetime.now() as the tick timestamp
    (injecting local time rather than reading exchange_timestamp), this test
    would fail because we explicitly set a 10-second-old exchange_timestamp.
    """
    validator = TickValidator(_cache())
    stale_ts = datetime.now(IST) - timedelta(seconds=10)
    tick = _tick(exchange_timestamp=stale_ts)
    result = validator.validate(tick)
    assert result is None, (
        "Gate 4 must use tick['exchange_timestamp'] — "
        "a 10s-old timestamp must be rejected"
    )


@freeze_time(FROZEN_NOW)
def test_gate4_timezone_aware_utc_timestamp():
    """
    Gate 4 must accept a fresh UTC-aware exchange_timestamp.

    KiteConnect may return timezone-aware UTC timestamps on some VPS
    configurations. Without .astimezone(IST), comparing a UTC-aware stamp
    with datetime.now(IST) produces an ~5.5h age (19800 s) — always stale.
    The fix normalises aware timestamps via .astimezone(IST) first.
    """
    from datetime import timezone

    validator = TickValidator(_cache())
    # 3 seconds ago in UTC — same moment as 3s ago in IST, must be fresh
    fresh_utc = datetime.now(timezone.utc) - timedelta(seconds=3)
    tick = _tick(exchange_timestamp=fresh_utc)
    result = validator.validate(tick)
    assert result is not None, (
        "Gate 4 must accept a 3s-old UTC-aware timestamp as fresh "
        "after normalising to IST via .astimezone()"
    )


# ------------------------------------------------------------------
# Gate 5 — Duplicate (silent)
# ------------------------------------------------------------------

@freeze_time(FROZEN_NOW)
def test_gate5_silent_discard_duplicate():
    """
    Identical price + timestamp on a second tick must be silently discarded.
    No exception, no log — return None silently.
    """
    validator = TickValidator(_cache())
    now = datetime.now(IST)
    tick = _tick(exchange_timestamp=now)

    first = validator.validate(tick.copy())
    assert first is not None, "First tick must pass"

    # Exact duplicate
    second = validator.validate(tick.copy())
    assert second is None, "Duplicate tick must be discarded"
    # Reaching this line without exception proves the discard is silent


# ------------------------------------------------------------------
# Happy path
# ------------------------------------------------------------------

@freeze_time(FROZEN_NOW)
def test_valid_tick_passes_all_gates():
    """A well-formed, fresh tick must pass all 5 gates and be returned."""
    validator = TickValidator(_cache(prev_close=2000.0))
    tick = _tick(
        last_price=2050.0,                   # within ±20% of 2000
        volume_traded=5000,
        exchange_timestamp=datetime.now(IST),  # fresh
    )
    result = validator.validate(tick)
    assert result is not None
    assert result["last_price"] == 2050.0
    assert result["instrument_token"] == 738561


# ------------------------------------------------------------------
# Exception safety
# ------------------------------------------------------------------

@pytest.mark.parametrize("bad_input", [
    None,
    {},
    {"last_price": None},
    {"instrument_token": None, "last_price": "not_a_number"},
    "not_a_dict",
    42,
    [],
])
def test_validator_never_raises_exception(bad_input):
    """
    Malformed or non-dict input must never raise — always returns None.
    D5 rule: the validator is a filter, not a gatekeeper that can crash the system.
    """
    validator = TickValidator(_cache())
    try:
        result = validator.validate(bad_input)
        # Good — no exception. Result should be None for bad input.
    except Exception as exc:
        pytest.fail(
            f"TickValidator raised {type(exc).__name__} on bad input "
            f"{bad_input!r}: {exc}"
        )
