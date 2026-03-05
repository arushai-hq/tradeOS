# Zerodha Positions API Payload

## How to Call

```python
# ALWAYS wrap in asyncio.to_thread() — this is a blocking HTTP call
positions_data = await asyncio.to_thread(kite.positions)
```

`kite.positions()` returns a dict with two keys: `"net"` and `"day"`.

```python
{
    "net": [<list of net position dicts>],
    "day": [<list of intraday position dicts>]
}
```

**TradeOS uses `"day"` positions for reconciliation.** Phase 1 is MIS-only intraday; `"day"` captures exactly today's intraday activity. `"net"` silently includes overnight NRML/CNC positions not managed by TradeOS Phase 1 — always use `"day"`.

## Full Payload Example

```python
positions_data = {
    "net": [
        {
            "tradingsymbol": "RELIANCE",
            "exchange": "NSE",
            "instrument_token": 738561,
            "product": "MIS",
            "quantity": 10,                # net quantity (positive=long, negative=short, 0=flat)
            "overnight_quantity": 0,       # quantity from previous trading day
            "multiplier": 1,
            "average_price": 2450.50,      # average entry price
            "close_price": 2440.00,        # previous day close
            "last_price": 2460.00,         # current LTP
            "value": 24605.0,              # quantity * last_price
            "pnl": 95.0,                   # unrealised + realised
            "m2m": 95.0,                   # mark to market
            "unrealised": 95.0,
            "realised": 0.0,
            "buy_quantity": 10,
            "buy_price": 2450.50,
            "buy_value": 24505.0,
            "sell_quantity": 0,
            "sell_price": 0.0,
            "sell_value": 0.0,
            "day_buy_quantity": 10,
            "day_buy_price": 2450.50,
            "day_buy_value": 24505.0,
            "day_sell_quantity": 0,
            "day_sell_price": 0.0,
            "day_sell_value": 0.0,
        },
        {
            "tradingsymbol": "INFY",
            "exchange": "NSE",
            "instrument_token": 408065,
            "product": "MIS",
            "quantity": 0,                 # flat position — ignore in reconciliation
            "average_price": 0.0,
            # ... other fields
        }
    ],
    "day": [
        # Same structure as "net" but filtered to today's activity only
    ]
}
```

## Key Fields for Reconciliation

| Field | Type | Used for |
|-------|------|---------|
| `tradingsymbol` | str | Primary key for reconciliation lookup — matches `open_positions` keys |
| `instrument_token` | int | Secondary — used to build `broker_token_by_symbol` for mismatch locking |
| `quantity` | int | The reconciliation signal — compare against local state |
| `product` | str | Should always be "MIS" (intraday) in Phase 1 |
| `average_price` | float | Not used for reconciliation — only for display |

## How to Parse for Reconciliation

```python
def _parse_broker_positions(positions_data: dict) -> dict[str, dict]:
    """
    Returns {tradingsymbol: position_dict} for positions with non-zero quantity.
    Always use positions_data["day"] — MIS intraday only.
    Never use ["net"] in Phase 1 — it includes overnight NRML/CNC positions
    not managed by TradeOS, which would generate false reconciliation mismatches.
    Ignores flat positions (quantity == 0).
    """
    return {
        pos["tradingsymbol"]: pos
        for pos in positions_data.get("day", [])  # Always ["day"] — never ["net"]
        if pos["quantity"] != 0
    }
```

## Edge Cases

| Scenario | What kite.positions() returns | Handling |
|----------|-------------------------------|---------|
| No positions open | `{"net": [], "day": []}` | Clean — no mismatches possible |
| Flat position (bought and sold same qty today) | Position with `quantity: 0` | Skip — filter out zero-qty positions |
| Short position | Position with negative `quantity` | Valid — compare as-is |
| Connection error | Raises `KiteException` | Wrap in try/except; do not proceed with reconciliation |
| Rate limit | Raises `KiteException` with status 429 | Retry with backoff (max 3 attempts) |

## Rate Limit Handling

Zerodha throttles to ~3 req/sec. Reconciliation at startup + scheduled 30-min + post-disruption is well within limits. Do not add extra polling beyond these 4 triggers.

```python
async def _fetch_positions_with_retry(kite, max_retries: int = 3) -> dict:
    for attempt in range(max_retries):
        try:
            return await asyncio.to_thread(kite.positions)
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            wait_s = 2 ** attempt  # 1s, 2s, 4s
            log.warning("positions_fetch_retry", attempt=attempt + 1, error=str(e))
            await asyncio.sleep(wait_s)
```
