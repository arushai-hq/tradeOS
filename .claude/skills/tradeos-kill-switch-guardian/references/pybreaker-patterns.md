# pybreaker Patterns — TradeOS Kill Switch

## Installation

```
pip install pybreaker
```

## KillSwitch Class (canonical TradeOS implementation)

```python
import pybreaker
import asyncio
import structlog
from datetime import datetime, time
from typing import Optional
import pytz

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")


class KillSwitch:
    """
    TradeOS D1 Kill Switch — three-level emergency stop.
    One instance shared across all 5 asyncio tasks.
    """

    def __init__(self, shared_state: dict) -> None:
        self.state: dict = {
            "level": 0,
            "active": False,
            "reason": "",
            "triggered_at": Optional[datetime],
        }
        # shared_state read cache — kill_switch_level mirrors self.state["level"]
        # written atomically inside trigger() via self._lock (see below)
        self._shared_state = shared_state
        # pybreaker tracks API error thresholds
        self.api_breaker = pybreaker.CircuitBreaker(
            fail_max=5,
            reset_timeout=300,  # 5-minute window
            listeners=[self._on_api_breaker_open],
        )
        self._lock = asyncio.Lock()

    def _on_api_breaker_open(self, cb: pybreaker.CircuitBreaker,
                              old_state: str, new_state: str) -> None:
        """Called by pybreaker when circuit opens (> 5 failures)."""
        if new_state == pybreaker.STATE_OPEN:
            asyncio.create_task(
                self.trigger(level=2, reason="api_circuit_breaker_open")
            )

    def is_trading_allowed(self) -> bool:
        """
        Gate check — every order path calls this before executing.
        Returns True ONLY when kill switch is inactive (level 0).
        """
        return not self.state["active"]  # same as: self.state["level"] == 0

    async def trigger(self, level: int, reason: str) -> None:
        """
        Single entry point for all kill switch triggers.

        Atomic dual-write pattern: this method is the ONLY place that writes
        shared_state["kill_switch_level"]. It updates both self.state (authoritative)
        and shared_state["kill_switch_level"] (read cache) while holding self._lock,
        so they are always in sync.

        risk_watchdog calls kill_switch.trigger() — it never writes
        shared_state["kill_switch_level"] directly.
        """
        async with self._lock:
            current = self.state["level"]

            # Never downgrade an active kill switch
            if current > 0 and level <= current:
                log.warning("kill_switch_downgrade_ignored",
                            current=current, requested=level)
                return

            self.state["level"] = level
            self.state["active"] = True
            self.state["reason"] = reason
            self.state["triggered_at"] = datetime.now(tz=IST)

            # Mirror level to shared_state read cache atomically (same lock)
            self._shared_state["kill_switch_level"] = level

            log.critical("kill_switch_triggered",
                         level=level, reason=reason,
                         triggered_at=self.state["triggered_at"].isoformat())

            await self._send_telegram_alert(
                f"🚨 KILL SWITCH LEVEL {level} — {reason}"
            )

            if level >= 2:
                await self._execute_level2_actions()

            if level == 3:
                log.critical("system_stop_halting_event_loop", level=3)
                asyncio.get_event_loop().stop()

    async def _execute_level2_actions(self) -> None:
        """Cancel all open orders and close all positions."""
        # Import here to avoid circular imports
        from execution_engine.order_manager import cancel_all_orders
        from execution_engine.position_manager import close_all_positions

        try:
            await cancel_all_orders()
            log.critical("level2_orders_cancelled")
        except Exception as e:
            log.error("level2_cancel_failed", error=str(e))

        try:
            await close_all_positions()
            log.critical("level2_positions_closed")
        except Exception as e:
            log.error("level2_close_failed", error=str(e))

    def reset(self) -> bool:
        """Manual reset — rejected during market hours."""
        now = datetime.now(tz=IST).time()
        if time(9, 15) <= now <= time(15, 30):
            log.warning("kill_switch_reset_rejected",
                        reason="market_hours_active")
            return False

        self.state = {
            "level": 0,
            "active": False,
            "reason": "",
            "triggered_at": None,
        }
        # Keep shared_state read cache in sync
        self._shared_state["kill_switch_level"] = 0
        log.info("kill_switch_reset", operator="manual")
        return True

    async def _send_telegram_alert(self, message: str) -> None:
        """Send Telegram alert — non-blocking."""
        # Import from notifications module
        from risk_manager.notifier import send_telegram
        try:
            await send_telegram(message)
        except Exception as e:
            log.error("telegram_alert_failed", error=str(e))

    async def call_with_breaker(self, coro):
        """Wrap an API call with the pybreaker circuit breaker."""
        return await asyncio.to_thread(self.api_breaker, lambda: coro)
```

## Wiring into the Async System

`KillSwitch` requires `shared_state` at construction so `trigger()` can atomically
update `shared_state["kill_switch_level"]`. Always create it after `_init_shared_state()`.

```python
# In main.py — create shared_state first, then KillSwitch
shared_state = _init_shared_state()
kill_switch = KillSwitch(shared_state=shared_state)  # binds to the same dict

async def main():
    await asyncio.gather(
        websocket_listener_task(kill_switch, shared_state),
        signal_processor_task(kill_switch, shared_state),
        order_monitor_task(kill_switch, shared_state),
        risk_watchdog_task(kill_switch, shared_state),
        heartbeat_task(kill_switch, shared_state),
    )
```

## Order Gate Pattern

Every function that places an order must start with this gate:

```python
async def place_order(symbol: str, qty: int, kill_switch: KillSwitch) -> None:
    # Gate check — non-negotiable
    if not kill_switch.is_trading_allowed():
        log.warning("order_blocked_by_kill_switch",
                    symbol=symbol, level=kill_switch.state["level"])
        return

    # Proceed with order placement only if allowed
    await kite.place_order(...)
```

## pybreaker State Machine

```
CLOSED (normal) ──[fail_max exceeded]──► OPEN (blocking)
    ▲                                         │
    │                                    [reset_timeout]
    │                                         ▼
    └──────────[success]────────── HALF-OPEN (testing)
```

- **CLOSED**: Calls pass through normally
- **OPEN**: All calls immediately raise `CircuitBreakerError`
- **HALF-OPEN**: One test call allowed; success → CLOSED, failure → OPEN

The `_on_api_breaker_open` listener fires when the circuit opens, triggering Level 2.
