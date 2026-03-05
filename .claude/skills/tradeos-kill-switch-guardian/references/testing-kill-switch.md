# Testing the Kill Switch — TradeOS pytest Patterns

## Fixtures

```python
# tests/conftest.py or top of test file
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, time
import pytz

IST = pytz.timezone("Asia/Kolkata")


@pytest.fixture
def kill_switch():
    """Fresh KillSwitch instance for each test."""
    from risk_manager.kill_switch import KillSwitch
    return KillSwitch()


@pytest.fixture
def active_level1_ks():
    """Kill switch pre-triggered at Level 1."""
    from risk_manager.kill_switch import KillSwitch
    ks = KillSwitch()
    ks.state["level"] = 1
    ks.state["active"] = True
    ks.state["reason"] = "test_fixture"
    ks.state["triggered_at"] = datetime.now(tz=IST)
    return ks


@pytest.fixture
def market_hours_time():
    """Patch datetime to return a time inside market hours (11:00 IST)."""
    with patch("risk_manager.kill_switch.datetime") as mock_dt:
        mock_dt.now.return_value = MagicMock(
            time=lambda: time(11, 0)
        )
        yield mock_dt


@pytest.fixture
def outside_market_hours_time():
    """Patch datetime to return a time outside market hours (17:00 IST)."""
    with patch("risk_manager.kill_switch.datetime") as mock_dt:
        mock_dt.now.return_value = MagicMock(
            time=lambda: time(17, 0)
        )
        yield mock_dt


@pytest.fixture
def mock_telegram(monkeypatch):
    """Suppress real Telegram alerts in tests."""
    mock = AsyncMock()
    monkeypatch.setattr("risk_manager.kill_switch.KillSwitch._send_telegram_alert", mock)
    return mock


@pytest.fixture
def mock_level2_actions(monkeypatch):
    """Suppress real order cancellation in tests."""
    mock = AsyncMock()
    monkeypatch.setattr("risk_manager.kill_switch.KillSwitch._execute_level2_actions", mock)
    return mock
```

## Test: Level 1 — Consecutive Losses

```python
@pytest.mark.asyncio
async def test_level1_triggered_by_consecutive_losses(kill_switch, mock_telegram):
    """Level 1 sets stop_new_signals, does NOT close positions."""
    from risk_manager.risk_manager import check_consecutive_losses

    trade_history = [
        {"pnl": -500}, {"pnl": -300}, {"pnl": -200}  # 3 consecutive losses
    ]
    check_consecutive_losses(trade_history, kill_switch.state)

    assert kill_switch.state["level"] == 1
    assert kill_switch.state["active"] is True
    assert kill_switch.state["reason"] == "3_consecutive_losses"
    assert not kill_switch.is_trading_allowed()


async def test_level1_does_not_close_positions(kill_switch, mock_telegram, mock_level2_actions):
    """Ensure Level 2 actions are NOT triggered by a Level 1 event."""
    await kill_switch.trigger(level=1, reason="test_consecutive_losses")

    mock_level2_actions.assert_not_called()  # positions must stay open
    assert kill_switch.state["level"] == 1
```

## Test: Level 2 — Daily Loss Circuit Breaker

```python
@pytest.mark.asyncio
async def test_level2_daily_loss_triggers(kill_switch, mock_telegram, mock_level2_actions):
    """Level 2 fires when daily_pnl_pct <= -0.03."""
    from risk_manager.risk_manager import check_daily_loss

    daily_pnl = -15_500  # -3.1% of ₹5L
    check_daily_loss(daily_pnl, kill_switch.state)

    assert kill_switch.state["level"] == 2
    assert kill_switch.state["active"] is True
    mock_level2_actions.assert_called_once()


@pytest.mark.asyncio
async def test_level2_does_not_trigger_below_threshold(kill_switch, mock_telegram):
    """Ensure Level 2 does NOT fire when loss is under 3%."""
    from risk_manager.risk_manager import check_daily_loss

    daily_pnl = -14_000  # -2.8% — below threshold
    check_daily_loss(daily_pnl, kill_switch.state)

    assert kill_switch.state["level"] == 0
    assert kill_switch.is_trading_allowed() is True
```

## Test: is_trading_allowed() Gate

```python
def test_is_trading_allowed_returns_true_when_inactive(kill_switch):
    assert kill_switch.is_trading_allowed() is True

def test_is_trading_allowed_returns_false_for_level1(active_level1_ks):
    assert active_level1_ks.is_trading_allowed() is False

@pytest.mark.asyncio
async def test_is_trading_allowed_returns_false_for_level2(kill_switch, mock_telegram, mock_level2_actions):
    await kill_switch.trigger(level=2, reason="test")
    assert kill_switch.is_trading_allowed() is False

@pytest.mark.asyncio
async def test_order_blocked_when_kill_switch_active(kill_switch, mock_telegram):
    """Order placement function must respect the gate."""
    from execution_engine.order_manager import place_order

    await kill_switch.trigger(level=1, reason="test")

    with patch("execution_engine.order_manager.kite") as mock_kite:
        await place_order("RELIANCE", qty=10, kill_switch=kill_switch)
        mock_kite.place_order.assert_not_called()
```

## Test: No Auto-Reset During Market Hours

```python
def test_reset_rejected_during_market_hours(active_level1_ks, market_hours_time):
    result = active_level1_ks.reset()
    assert result is False
    assert active_level1_ks.state["active"] is True  # still active

def test_reset_allowed_outside_market_hours(active_level1_ks, outside_market_hours_time):
    result = active_level1_ks.reset()
    assert result is True
    assert active_level1_ks.state["level"] == 0
    assert active_level1_ks.state["active"] is False
```

## Test: Level 3 Includes Level 2 Actions

```python
@pytest.mark.asyncio
async def test_level3_executes_level2_actions_first(kill_switch, mock_telegram, mock_level2_actions):
    """Level 3 must call Level 2 actions before halting."""
    with patch("asyncio.get_event_loop") as mock_loop:
        mock_loop.return_value.stop = MagicMock()
        await kill_switch.trigger(level=3, reason="manual_telegram_override")

    # Level 2 must run before loop.stop()
    mock_level2_actions.assert_called_once()
    mock_loop.return_value.stop.assert_called_once()
    assert kill_switch.state["level"] == 3


@pytest.mark.asyncio
async def test_level3_without_level2_is_blocked():
    """Direct Level 3 trigger always runs Level 2 actions."""
    from risk_manager.kill_switch import KillSwitch
    ks = KillSwitch()
    ks._execute_level2_actions = AsyncMock()

    with patch("asyncio.get_event_loop"):
        await ks.trigger(level=3, reason="test")

    ks._execute_level2_actions.assert_called_once()
```

## Test: WebSocket Disconnect Trigger

```python
@pytest.mark.asyncio
async def test_websocket_disconnect_triggers_level2_after_60s(
    kill_switch, mock_telegram, mock_level2_actions, market_hours_time
):
    """WS disconnect > 60 seconds during market hours → Level 2."""
    from data_engine.websocket_listener import monitor_websocket_disconnect

    ws_state = {
        "connected": False,
        "disconnect_start": datetime.now(tz=IST) - timedelta(seconds=61),
    }
    await monitor_websocket_disconnect(ws_state, kill_switch.state)

    assert kill_switch.state["level"] == 2
    assert kill_switch.state["reason"] == "ws_disconnected_60s"
```

## Running Tests

```bash
pytest tests/test_kill_switch.py -v --tb=short
pytest tests/test_kill_switch.py -k "level2" -v  # run only Level 2 tests
pytest tests/ --cov=risk_manager --cov-report=term-missing  # coverage report
```

**Minimum coverage requirement:** 90% for `risk_manager/kill_switch.py`
