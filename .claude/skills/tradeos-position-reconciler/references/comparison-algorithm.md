# Position Comparison Algorithm

The comparison algorithm takes broker positions (from Zerodha) and local positions (from `shared_state["position_state"]`) and returns a list of mismatches.

## Data Structures

```python
# Broker position record (from kite.positions()["net"])
BrokerPosition = {
    "tradingsymbol": str,          # e.g. "RELIANCE"
    "instrument_token": int,       # e.g. 738561
    "quantity": int,               # net qty (positive=long, negative=short, 0=flat)
    "average_price": float,        # average entry price
    "pnl": float,                  # unrealised + realised
    "unrealised": float,
    "realised": float,
    "exchange": str,               # "NSE" or "BSE"
    "product": str,                # "MIS" for intraday
}

# Local position record (in shared_state["position_state"])
LocalPosition = {
    "instrument_token": int,
    "tradingsymbol": str,
    "quantity": int,
    "average_price": float,
    "entry_time": datetime,
    "strategy": str,               # "s1" etc
}

# Mismatch record
Mismatch = {
    "instrument_token": int,
    "tradingsymbol": str,
    "mismatch_type": str,          # "qty_mismatch" | "ghost_position" | "missing_local"
    "broker_qty": int,
    "local_qty": int,
    "severity": str,               # "warning" | "critical"
}
```

## The Comparison Function

```python
def _compare_positions(
    broker_positions: list[dict],
    local_positions: dict[int, dict],
) -> list[dict]:
    """
    Compare broker positions vs local state.
    Returns list of Mismatch dicts. Empty list = clean.

    Rules:
    - Broker qty != 0 and no local record → Ghost position (CRITICAL)
    - Local qty != 0 and broker qty == 0 → Missing at broker (WARNING)
    - Both have qty but quantities differ → Qty mismatch (WARNING)
    - Both have qty == 0 → clean (skip)
    """
    mismatches = []

    # Build a set of broker tokens with non-zero quantity
    broker_by_token: dict[int, dict] = {
        pos["instrument_token"]: pos
        for pos in broker_positions
        if pos["quantity"] != 0
    }

    # Check every broker position against local state
    for token, broker_pos in broker_by_token.items():
        local_pos = local_positions.get(token)

        if local_pos is None:
            # Broker has position, we have no record → ghost
            mismatches.append({
                "instrument_token": token,
                "tradingsymbol": broker_pos["tradingsymbol"],
                "mismatch_type": "ghost_position",
                "broker_qty": broker_pos["quantity"],
                "local_qty": 0,
                "severity": "critical",
            })
        elif local_pos["quantity"] != broker_pos["quantity"]:
            # Both have position but quantities differ
            mismatches.append({
                "instrument_token": token,
                "tradingsymbol": broker_pos["tradingsymbol"],
                "mismatch_type": "qty_mismatch",
                "broker_qty": broker_pos["quantity"],
                "local_qty": local_pos["quantity"],
                "severity": "warning",
            })

    # Check every local position against broker
    for token, local_pos in local_positions.items():
        if local_pos["quantity"] == 0:
            continue
        if token not in broker_by_token:
            # We think we have position, broker says flat
            mismatches.append({
                "instrument_token": token,
                "tradingsymbol": local_pos["tradingsymbol"],
                "mismatch_type": "missing_local",
                "broker_qty": 0,
                "local_qty": local_pos["quantity"],
                "severity": "warning",
            })

    return mismatches
```

## Severity Rules

| Mismatch Type | Severity | Action |
|---------------|----------|--------|
| `ghost_position` | CRITICAL | Lock instrument, log CRITICAL, Telegram CRITICAL alert, never auto-close |
| `qty_mismatch` | WARNING | Lock instrument, log WARNING, Telegram WARNING alert |
| `missing_local` | WARNING | Lock instrument, log WARNING, update local state to 0 if auto-adjust enabled |

## What Is Never Compared

- Average price differences: broker avg_price vs local avg_price are NOT compared. Minor rounding differences are expected. Only quantity is the reconciliation signal.
- Realised PnL: broker realised PnL is not used for reconciliation — only for display.
- Orders in flight: open/pending orders are not positions. Only filled quantities.
