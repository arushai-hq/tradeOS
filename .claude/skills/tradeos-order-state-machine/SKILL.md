---
name: tradeos-order-state-machine
description: TradeOS D2 order state machine implementation enforcer. Use this skill whenever implementing, reviewing, or debugging order lifecycle tracking, order state transitions, Zerodha status mapping, startup reconciliation, duplicate order prevention, or async state machine patterns in the TradeOS codebase. Invoke for tasks like: "implement order state machine", "track order from placement to fill", "handle Zerodha order status updates", "prevent duplicate orders for same symbol", "write startup reconciliation before placing orders", "map kite COMPLETE/OPEN/CANCELLED to local states", "handle PARTIALLY_FILLED orders", "on_enter_FILLED callback", "on_enter_REJECTED callback", "UNKNOWN order on restart", "write InvalidStateTransition", "order state dict keyed by order_id". This skill encodes the exact 8-state hierarchy, valid transitions, Zerodha status mappings, and restart-safety protocol that the base model does NOT know without it. Do NOT invoke for Redux state machines, user authentication flows, database migrations, React components, or general finite state machine theory that is not TradeOS order-specific.
related-skills: python-pro, tradeos-kill-switch-guardian, test-master
---

# TradeOS Order State Machine (D2)

An order in TradeOS is NOT binary. It has 8 states, and every transition must be tracked, logged, and handled. This skill enforces D2 reliability discipline — the rule that prevents us from treating a PLACED order as FILLED, which is how capital gets lost.

## The 8 States

```
CREATED → SUBMITTED → ACKNOWLEDGED → PARTIALLY_FILLED → FILLED
                                   → REJECTED
                                   → PENDING_CANCEL  → CANCELLED
                                   → PENDING_UPDATE  → ACKNOWLEDGED
                                   → EXPIRED
                                   → UNKNOWN          (restart artifact)
```

**Terminal states (no further transitions):** FILLED, CANCELLED, REJECTED, EXPIRED, UNKNOWN

## Core Rules (non-negotiable)

1. **Invalid transitions raise** — `InvalidStateTransition` exception, always logged CRITICAL
2. **Restart safety** — `system_ready = False` until Zerodha reconciliation passes with zero UNKNOWN orders
3. **No duplicates** — If an ACTIVE order exists for a symbol, raise `DuplicateOrderError` before calling `kite.place_order()`
4. **PARTIALLY_FILLED ≠ FILLED** — instrument lock NOT released, position tracking reflects partial qty only
5. **All transitions log** — `{order_id, symbol, strategy, from_state, to_state, timestamp}` via structlog

## State Storage

```python
# Global dict shared across async tasks — keyed by order_id
order_registry: dict[str, OrderStateMachine] = {}

# ACTIVE states — any order NOT in this set is still live
TERMINAL_STATES = {"FILLED", "CANCELLED", "REJECTED", "EXPIRED", "UNKNOWN"}
```

## Reference Files

| File | When to read |
|------|-------------|
| `references/state-definitions.md` | Implementing transitions, callbacks, InvalidStateTransition |
| `references/zerodha-status-mapping.md` | Mapping Zerodha API status strings to TradeOS states |
| `references/restart-safety-protocol.md` | Writing startup reconciliation, system_ready flag, UNKNOWN handling |
| `references/duplicate-prevention.md` | DuplicateOrderError, pre-order gate check |
| `references/async-statemachine-patterns.md` | python-statemachine + asyncio, non-blocking callbacks |

## What This Skill Prevents

- Placing orders before startup reconciliation completes
- Two active orders for the same symbol simultaneously
- Silent swallowing of an invalid state transition
- Blocking the asyncio event loop from within a transition callback
- Treating PARTIALLY_FILLED as FILLED and releasing position locks prematurely
- `order_id` not found on restart being silently ignored (must become UNKNOWN + lock)
