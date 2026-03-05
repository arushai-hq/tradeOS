# Position Comparison Algorithm

The comparison algorithm takes broker positions (from Zerodha) and local positions
(from `shared_state["open_positions"]`) and returns a list of mismatches.

`open_positions` is owned by `order_monitor` (D6). D7 reads it as the local baseline —
it never writes to it directly.

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

# Local position record (from shared_state["open_positions"])
# Key is tradingsymbol (e.g. "RELIANCE") — value is this dict.
# open_positions is written by order_monitor only — never by D7.
LocalPosition = {
    "qty": int,            # net quantity (positive=long, negative=short)
    "avg_price": float,    # average entry price
    "side": str,           # "BUY" or "SELL"
    "order_id": str,       # order_id that created this position
    "entry_time": datetime,
}

# Mismatch record
Mismatch = {
    "instrument_token": int,       # from broker; 0 if not in broker history
    "tradingsymbol": str,
    "mismatch_type": str,          # "qty_mismatch" | "ghost_position" | "missing_local"
    "broker_qty": int,
    "local_qty": int,
    "severity": str,               # "warning" | "critical"
}
```

## Building the Local Map

```python
# D7 reads open_positions as the local baseline — never writes to it
local_map: dict[str, dict] = shared_state["open_positions"]
```

## The Comparison Function

```python
def _compare_positions(
    broker_positions: list[dict],
    local_positions: dict[str, dict],  # from shared_state["open_positions"]
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

    # Build broker maps — by symbol for position matching
    broker_by_symbol: dict[str, dict] = {
        pos["tradingsymbol"]: pos
        for pos in broker_positions
        if pos["quantity"] != 0
    }
    # Full broker map (including 0-qty entries) for token lookup on local-only mismatches
    broker_token_by_symbol: dict[str, int] = {
        pos["tradingsymbol"]: pos["instrument_token"]
        for pos in broker_positions
    }

    # Check every broker position against local state
    for symbol, broker_pos in broker_by_symbol.items():
        local_pos = local_positions.get(symbol)

        if local_pos is None:
            # Broker has position, we have no record → ghost
            mismatches.append({
                "instrument_token": broker_pos["instrument_token"],
                "tradingsymbol": symbol,
                "mismatch_type": "ghost_position",
                "broker_qty": broker_pos["quantity"],
                "local_qty": 0,
                "severity": "critical",
            })
        elif local_pos["qty"] != broker_pos["quantity"]:
            # Both have position but quantities differ
            mismatches.append({
                "instrument_token": broker_pos["instrument_token"],
                "tradingsymbol": symbol,
                "mismatch_type": "qty_mismatch",
                "broker_qty": broker_pos["quantity"],
                "local_qty": local_pos["qty"],
                "severity": "warning",
            })

    # Check every local position against broker
    for symbol, local_pos in local_positions.items():
        if local_pos["qty"] == 0:
            continue
        if symbol not in broker_by_symbol:
            # We think we have position, broker says flat
            mismatches.append({
                "instrument_token": broker_token_by_symbol.get(symbol, 0),
                "tradingsymbol": symbol,
                "mismatch_type": "missing_local",
                "broker_qty": 0,
                "local_qty": local_pos["qty"],
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
