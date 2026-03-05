---
name: tradeos-tick-validator
description: >
  TradeOS D5 tick validation enforcer — 5-gate pipeline that filters bad ticks
  from the Zerodha KiteConnect feed before any strategy logic sees them.

  Use this skill whenever implementing: the TickValidator class or any tick
  validation logic, the zero-price filter (Gate 1), NSE ±20% circuit breaker
  filter (Gate 2), negative volume filter (Gate 3), staleness filter using
  exchange_timestamp (Gate 4), duplicate tick suppression (Gate 5), bad tick
  monitoring counters, or the prev_close_cache for circuit breaker reference data.

  Invoke for tasks like: "validate tick before passing to strategy",
  "filter bad ticks from KiteConnect feed", "implement the 5-gate tick
  validator", "detect stale ticks using exchange timestamp", "NSE circuit
  breaker tick filter", "track bad tick rate per instrument", "write the
  TickValidator class", "duplicate tick detection", "prev close cache for
  tick validation", "bad tick rate alert at 50 per hour".

  Do NOT invoke for: web form validation, CSV import validation, API response
  schema validation, request middleware validation, or any non-tick validation.
related-skills: python-pro, tradeos-websocket-resilience, tradeos-observability, tradeos-kill-switch-guardian
---

# TradeOS D5 — Tick Validator

Every tick from Zerodha KiteConnect must pass all 5 gates before strategy
logic can see it. The validator is a pure filter — it never raises exceptions,
never halts the system, never calls external services.

## The 5 Gates (execute in this exact order)

| Gate | Name | Condition | Failure action |
|------|------|-----------|----------------|
| 1 | Zero price | `last_price > 0` | Discard + WARNING log |
| 2 | Circuit breaker | `abs(price - prev_close) / prev_close <= 0.20` | Discard + WARNING log |
| 3 | Negative volume | `volume_traded >= 0` | Discard + WARNING log |
| 4 | Staleness | `age_seconds <= 5` (exchange_timestamp) | Discard + WARNING log |
| 5 | Duplicate | `price != last_tick[sym] OR timestamp != last_tick[sym]` | Silent discard (no log) |

First failure = discard immediately. Never reorder gates.

## Reference Routing

| Task | Read |
|------|------|
| Gate implementations, pipeline code | `references/five-gate-pipeline.md` |
| KiteConnect tick fields + subscription modes | `references/zerodha-tick-payload.md` |
| prev_close_cache loading + refresh rules | `references/prev-close-cache.md` |
| Bad tick counters + hourly alerts | `references/bad-tick-monitoring.md` |
| Performance constraints (< 1ms requirement) | `references/performance-constraints.md` |

## Core Rules (never violate)

**Never raise from the validator.** Every gate returns `bool`. A bad tick
causes `return False` — not `raise`. The call site checks the return value
and skips the tick. The system continues.

**Gate 5 is the only silent failure.** All other gates log WARNING when they
discard. Gate 5 (duplicate) discards silently — logging every duplicate
would spam logs on reconnect.

**prev_close unavailable → pass Gate 2.** If the cache doesn't have a close
price for the instrument, skip the circuit breaker check (return True for
Gate 2). Never block ticks on missing reference data.

**None fields fail gates.** If `tick.last_price is None`, Gate 1 fails.
If `tick.volume_traded is None`, Gate 3 fails. Absent fields are treated
as invalid data.

**exchange_timestamp only for Gate 4.** Never use `datetime.now()` to
compute tick age — use `tick.exchange_timestamp` vs `datetime.now(IST)`.

## Quick Reference

```python
import structlog
import pytz
from datetime import datetime

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")

class TickValidator:
    def __init__(self):
        self.prev_close_cache: dict[int, float] = {}  # token → prev_close
        self.last_tick: dict[int, dict] = {}           # token → {price, ts}
        self.bad_tick_count: dict[int, dict[int, int]] = {}  # token → {gate: count}

    def validate(self, tick) -> bool:
        """Returns True if tick passes all 5 gates. False = discard."""
        token = tick.instrument_token
        if not self._gate1_zero_price(tick): return False
        if not self._gate2_circuit_breaker(tick): return False
        if not self._gate3_negative_volume(tick): return False
        if not self._gate4_staleness(tick): return False
        if not self._gate5_duplicate(tick): return False
        self.last_tick[token] = {
            "price": tick.last_price,
            "ts": tick.exchange_timestamp,
        }
        return True
```
