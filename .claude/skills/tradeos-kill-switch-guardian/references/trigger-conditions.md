# Kill Switch Trigger Conditions — TradeOS D1

## Market Hours Definition

```python
from datetime import time
import pytz

IST = pytz.timezone("Asia/Kolkata")
MARKET_OPEN  = time(9, 15)   # 09:15 IST
MARKET_CLOSE = time(15, 30)  # 15:30 IST

def is_market_hours() -> bool:
    now = datetime.now(tz=IST).time()
    return MARKET_OPEN <= now <= MARKET_CLOSE
```

---

## Level 1 Triggers

### L1-T1: Three Consecutive Losing Trades

```python
# In RiskManager — tracked per session, reset on system restart
trade_history: list[dict] = []   # appended after every fill

def check_consecutive_losses(trade_history: list, kill_switch_state: dict) -> None:
    """Call after every trade fill event."""
    if len(trade_history) < 3:
        return
    last_three = trade_history[-3:]
    if all(t["pnl"] < 0 for t in last_three):
        trigger_kill_switch(kill_switch_state, level=1,
                            reason="3_consecutive_losses")
```

- Tracks `pnl` (realised P&L per trade after fill)
- Three consecutive negatives → Level 1
- Does NOT require the losses to exceed any threshold — any negative counts

### L1-T2: Daily Loss Exceeds 3%

```python
# Constants from config/settings.yaml — never accept as runtime params
MAX_DAILY_LOSS_PCT = 0.030   # 3.0% of total capital
TOTAL_CAPITAL = 500_000      # ₹5L

def check_daily_loss(daily_pnl: float, kill_switch_state: dict) -> None:
    """Called by Risk Watchdog every 1 second."""
    daily_pnl_pct = daily_pnl / TOTAL_CAPITAL
    if daily_pnl_pct <= -MAX_DAILY_LOSS_PCT:
        trigger_kill_switch(kill_switch_state, level=1,
                            reason=f"daily_loss_{daily_pnl_pct:.2%}")
```

- `daily_pnl` is cumulative realised P&L for the trading session
- Threshold: `daily_pnl_pct <= -0.03` (loss of ₹15,000 on ₹5L capital)
- Reset daily at session start (midnight or first tick of day)

---

## Level 2 Triggers

### L2-T1: WebSocket Disconnected > 60 Seconds During Market Hours

```python
async def monitor_websocket_disconnect(
    ws_state: dict, kill_switch_state: dict
) -> None:
    """Runs in WebSocket Listener task. Checks disconnect duration."""
    if ws_state["connected"]:
        ws_state["disconnect_start"] = None
        return

    # Only trigger during market hours
    if not is_market_hours():
        return

    if ws_state["disconnect_start"] is None:
        ws_state["disconnect_start"] = datetime.now(tz=IST)
        return

    elapsed = (datetime.now(tz=IST) - ws_state["disconnect_start"]).seconds
    if elapsed >= 60:
        log.critical("websocket_disconnect_trigger", elapsed_seconds=elapsed)
        await trigger_kill_switch(kill_switch_state, level=2,
                                  reason="ws_disconnected_60s")
```

- Only fires during market hours (09:15–15:30 IST)
- 60-second threshold is hardcoded — not configurable
- Uses `asyncio.sleep()` loop or task monitor, never `time.sleep()`

### L2-T2: Zerodha API Errors > 5 in 5-Minute Rolling Window

```python
from collections import deque

api_error_timestamps: deque = deque()  # rolling window of error datetimes

def record_api_error(kill_switch_state: dict) -> None:
    """Call on every Zerodha API exception."""
    now = datetime.now(tz=IST)
    api_error_timestamps.append(now)

    # Purge errors older than 5 minutes
    cutoff = now - timedelta(minutes=5)
    while api_error_timestamps and api_error_timestamps[0] < cutoff:
        api_error_timestamps.popleft()

    if len(api_error_timestamps) > 5:
        trigger_kill_switch(kill_switch_state, level=2,
                            reason=f"api_errors_{len(api_error_timestamps)}_in_5min")
```

- 5-minute rolling window (not fixed 5-minute buckets)
- Threshold: strictly > 5 (6th error triggers)
- Covers: `NetworkException`, `TokenException`, `DataException` from pykiteconnect

### L2-T3: Position Mismatch Detected by Reconciliation

```python
# Called by ReconciliationModule after comparing local state vs Zerodha
def on_position_mismatch(symbol: str, kill_switch_state: dict) -> None:
    log.critical("position_mismatch_detected", symbol=symbol)
    trigger_kill_switch(kill_switch_state, level=2,
                        reason=f"position_mismatch_{symbol}")
```

---

## Level 3 Triggers

### L3-T1: Manual Telegram Command `/killswitch3`

```python
# In Telegram bot handler
async def handle_telegram_command(command: str, kill_switch_state: dict) -> None:
    if command == "/killswitch3":
        log.critical("manual_level3_triggered", source="telegram")
        await trigger_kill_switch(kill_switch_state, level=3,
                                  reason="manual_telegram_override")
```

### L3-T2: Unrecoverable Exception in Core Event Loop

```python
# In main asyncio runner
async def main():
    try:
        await asyncio.gather(
            websocket_listener_task(),
            signal_processor_task(),
            order_monitor_task(),
            risk_watchdog_task(),
            heartbeat_task(),
        )
    except Exception as e:
        log.critical("unrecoverable_exception", error=str(e), exc_info=True)
        await trigger_kill_switch(kill_switch_state, level=3,
                                  reason=f"unrecoverable_exception_{type(e).__name__}")
        raise
```

---

## Trigger Function (canonical implementation)

```python
async def trigger_kill_switch(
    kill_switch_state: dict,
    level: int,
    reason: str
) -> None:
    """Single entry point for all kill switch triggers."""
    current_level = kill_switch_state["level"]

    # Never downgrade
    if level <= current_level and current_level > 0:
        log.warning("kill_switch_downgrade_ignored",
                    current=current_level, requested=level)
        return

    kill_switch_state["level"] = level
    kill_switch_state["active"] = True
    kill_switch_state["reason"] = reason
    kill_switch_state["triggered_at"] = datetime.now(tz=IST)

    log.critical("kill_switch_triggered", level=level, reason=reason)
    await send_telegram_alert(f"KILL SWITCH LEVEL {level} — {reason}")

    if level >= 2:
        await _execute_level2_actions()
    if level == 3:
        asyncio.get_event_loop().stop()
```
