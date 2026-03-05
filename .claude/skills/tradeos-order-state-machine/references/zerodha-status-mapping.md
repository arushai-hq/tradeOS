# Zerodha Status Mapping — TradeOS D2

## Canonical Mapping Table

| Zerodha status string | TradeOS state | Notes |
|-----------------------|--------------|-------|
| `"OPEN"` | `ACKNOWLEDGED` | Order in broker queue |
| `"COMPLETE"` | `FILLED` | Full fill |
| `"CANCELLED"` | `CANCELLED` | User or system cancelled |
| `"REJECTED"` | `REJECTED` | Broker rejected |
| `"MODIFY PENDING"` | `PENDING_UPDATE` | Modify request in flight |
| `"CANCEL PENDING"` | `PENDING_CANCEL` | Cancel request in flight |
| `"TRIGGER PENDING"` | `ACKNOWLEDGED` | SL/SL-M order awaiting trigger price |

## Implementation

```python
from typing import Optional
import structlog

log = structlog.get_logger()

ZERODHA_STATUS_MAP: dict[str, str] = {
    "OPEN":           "ACKNOWLEDGED",
    "COMPLETE":       "FILLED",
    "CANCELLED":      "CANCELLED",
    "REJECTED":       "REJECTED",
    "MODIFY PENDING": "PENDING_UPDATE",
    "CANCEL PENDING": "PENDING_CANCEL",
    "TRIGGER PENDING": "ACKNOWLEDGED",
}


def map_zerodha_status(
    zerodha_status: str,
    order_id: str,
    symbol: str,
) -> str:
    """
    Map a Zerodha order status string to a TradeOS state.

    Unknown statuses are mapped to UNKNOWN with a CRITICAL log.
    Never returns None — always produces a valid TradeOS state string.
    """
    tradeos_state = ZERODHA_STATUS_MAP.get(zerodha_status.upper().strip())

    if tradeos_state is None:
        log.critical(
            "unknown_zerodha_status",
            zerodha_status=zerodha_status,
            order_id=order_id,
            symbol=symbol,
            mapped_to="UNKNOWN",
        )
        return "UNKNOWN"

    return tradeos_state


def sync_order_from_zerodha(
    order_data: dict,
    order_registry: dict,
) -> Optional[str]:
    """
    Given a Zerodha order dict (from kite.orders()), update the local
    OrderStateMachine to match Zerodha's reported state.

    Returns the new TradeOS state, or None if the order_id is not in registry.
    Called by: Order Monitor task (every 5s) + Startup Reconciliation.
    """
    order_id = order_data.get("order_id", "")
    symbol = order_data.get("tradingsymbol", "")
    zerodha_status = order_data.get("status", "")

    target_state = map_zerodha_status(zerodha_status, order_id, symbol)

    if order_id not in order_registry:
        # Order exists on Zerodha but NOT in local state — UNKNOWN
        log.critical(
            "unrecognised_order_on_sync",
            order_id=order_id,
            symbol=symbol,
            zerodha_status=zerodha_status,
        )
        return None  # Caller handles UNKNOWN creation

    order_sm = order_registry[order_id]
    current_state = order_sm.current_state.id

    # No-op if already in sync
    if current_state == target_state:
        return target_state

    # Drive state machine to match Zerodha
    _drive_to_state(order_sm, target_state, order_id, symbol)
    return target_state


def _drive_to_state(
    order_sm,
    target_state: str,
    order_id: str,
    symbol: str,
) -> None:
    """Apply the correct transition to reach target_state."""
    trigger_map = {
        "SUBMITTED":         "submit",
        "ACKNOWLEDGED":      "acknowledge",
        "PARTIALLY_FILLED":  "partial_fill",
        "FILLED":            "fill",
        "REJECTED":          "reject",
        "PENDING_CANCEL":    "request_cancel",
        "CANCELLED":         "confirm_cancel",
        "PENDING_UPDATE":    "request_update",
        "EXPIRED":           "expire",
        "UNKNOWN":           "mark_unknown",
    }
    trigger_name = trigger_map.get(target_state)
    if trigger_name is None:
        log.error("no_trigger_for_target_state",
                  target_state=target_state, order_id=order_id)
        return

    from execution_engine.order_registry import safe_transition
    safe_transition(order_sm, trigger_name)
```

## PARTIALLY_FILLED handling

Zerodha does not have a single "PARTIALLY_FILLED" status string — it reports
partial fills via the `filled_quantity` field while status remains `"OPEN"`.
The Order Monitor must detect this:

```python
def detect_partial_fill(order_data: dict) -> bool:
    """True if Zerodha reports a partial fill — status OPEN but some qty filled."""
    status = order_data.get("status", "")
    filled_qty = order_data.get("filled_quantity", 0)
    pending_qty = order_data.get("pending_quantity", 0)
    return status == "OPEN" and filled_qty > 0 and pending_qty > 0
```

When `detect_partial_fill()` returns True, trigger `partial_fill` on the state machine.
Do NOT call `fill` until `pending_quantity == 0`.
