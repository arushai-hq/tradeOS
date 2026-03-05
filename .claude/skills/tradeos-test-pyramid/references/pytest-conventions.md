# Pytest Conventions — TradeOS Unit Tests

## The 5 Mandatory Fixtures (conftest.py)

Every fixture here must exist in `tests/conftest.py`. They are used across all 5 unit test files.

```python
import pytest
from unittest.mock import MagicMock, AsyncMock
from datetime import datetime
import pytz

@pytest.fixture
def shared_state() -> dict:
    """Default shared state dict — same structure as _init_shared_state() in main.py."""
    return {
        "position_state": {},
        "locked_instruments": set(),
        "recon_in_progress": False,
        "kill_switch_level": 0,
        "daily_pnl_pct": 0.0,
        "consecutive_losses": 0,
        "open_positions": {},
        "ws_connected": True,
    }

@pytest.fixture
def kill_switch(shared_state):
    """KillSwitch instance at level 0 (trading allowed)."""
    from risk_manager.kill_switch import KillSwitch
    return KillSwitch(shared_state=shared_state)

@pytest.fixture
def mock_kite():
    """MagicMock of KiteConnect — never hits real API."""
    kite = MagicMock()
    kite.place_order = MagicMock(return_value="ORDER123")
    kite.cancel_order = MagicMock(return_value=True)
    kite.orders = MagicMock(return_value=[])
    kite.positions = MagicMock(return_value={"net": [], "day": []})
    return kite

@pytest.fixture
def valid_tick():
    """Well-formed Zerodha tick dict — passes all 5 validation gates."""
    import time
    return {
        "instrument_token": 738561,
        "tradingsymbol": "RELIANCE",
        "last_price": 2450.0,
        "volume_traded": 1500000,
        "exchange_timestamp": datetime.now(pytz.timezone("Asia/Kolkata")),
        "last_trade_time": datetime.now(pytz.timezone("Asia/Kolkata")),
        "ohlc": {"open": 2420.0, "high": 2470.0, "low": 2410.0, "close": 2430.0},
    }

@pytest.fixture
def ist_now():
    """Current datetime in Asia/Kolkata timezone (IST)."""
    return datetime.now(pytz.timezone("Asia/Kolkata"))
```

---

## Mocking Rules

These rules prevent tests from accidentally hitting real infrastructure:

**Always mock:**
- `kite.place_order()` — never hit the real Zerodha API
- `kite.orders()` — return controlled fixture data
- `kite.positions()` — return controlled fixture data
- Telegram `httpx.AsyncClient.post()` — no real HTTP calls
- `structlog.get_logger()` — or let it log to `/dev/null`

**Never mock:**
- The component under test itself
- Pure Python business logic (calculations, comparisons)
- State machine transitions

**Pattern for async mocks:**
```python
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_order_placed(mock_kite, shared_state):
    mock_kite.place_order = MagicMock(return_value="ORD001")
    # test the component that calls kite.place_order()
```

---

## Async Tests

All tests for async functions use `pytest.mark.asyncio`:

```python
import pytest

@pytest.mark.asyncio
async def test_signal_processor_skips_during_recon(shared_state):
    shared_state["recon_in_progress"] = True
    # ... test logic
```

Add to `pyproject.toml` or `pytest.ini`:
```ini
[pytest]
asyncio_mode = auto
```

---

## Time-Dependent Tests (freezegun)

For tests that depend on IST time (hard exit at 15:00, market hours check):

```python
from freezegun import freeze_time

@freeze_time("2024-01-15 09:30:00+05:30")  # IST 09:30
def test_orders_allowed_during_market_hours(kill_switch):
    assert kill_switch.can_reset() is True

@freeze_time("2024-01-15 15:05:00+05:30")  # IST 15:05 — after hard exit
def test_hard_exit_triggers_at_1500(risk_manager):
    assert risk_manager.is_past_hard_exit() is True
```

Never use `time.sleep()` in tests. Use `freezegun` for time travel and `asyncio.sleep()` mocking for async delays.

---

## Parametrize Patterns

Use `@pytest.mark.parametrize` for boundary value tests. The point is to test exactly at the threshold, one below, and one above.

**RSI boundary (S1 long signal):**
```python
@pytest.mark.parametrize("rsi,expect_signal", [
    (54, False),   # one below lower bound → no signal
    (55, True),    # lower bound → signal
    (70, True),    # upper bound → signal
    (71, False),   # one above upper bound → no signal
])
def test_rsi_boundary_for_long_signal(rsi, expect_signal, shared_state):
    # hold all other S1 conditions constant
    ...
```

**Daily loss threshold:**
```python
@pytest.mark.parametrize("daily_pnl_pct,expect_level2", [
    (-0.029, False),   # just under 3% → no trigger
    (-0.030, True),    # exactly 3% → trigger
    (-0.031, True),    # over 3% → trigger
])
def test_daily_loss_level2_boundary(daily_pnl_pct, expect_level2, shared_state):
    shared_state["daily_pnl_pct"] = daily_pnl_pct
    ...
```

**Max open positions:**
```python
@pytest.mark.parametrize("open_count,expect_allowed", [
    (2, True),   # 2 open → new entry allowed
    (3, False),  # 3 open → blocked
    (4, False),  # 4 open → blocked (shouldn't happen, but guard it)
])
def test_position_count_gate(open_count, expect_allowed, shared_state):
    shared_state["open_positions"] = {f"sym{i}": {} for i in range(open_count)}
    ...
```

---

## What Makes a Good Unit Test

The goal of each test is to document one specific invariant of the system. Tests should:

1. **Be readable as a specification** — the test name and body together explain the rule without needing external docs
2. **Test one thing** — a failure points immediately to what broke
3. **Cover edges, not just happy paths** — the happy path rarely breaks; edge cases do
4. **Run fast** — a test that takes > 1s is probably doing real I/O, fix it

Anti-patterns to avoid:
- `assert result is not None` — this tests nothing meaningful
- Testing implementation details (how it does it, not what it guarantees)
- Multiple `assert` statements testing unrelated things in one test
- Hard-coded real instrument tokens or prices in test data — use fixtures
