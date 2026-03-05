# Prev Close Cache — TradeOS D5

## Purpose

Gate 2 (circuit breaker) needs the previous day's close price to detect
prices that have moved more than ±20%. This reference data is loaded at
startup and never updated during market hours.

## Data Structure

```python
# Keyed by instrument_token (int), not symbol string
prev_close_cache: dict[int, float] = {}
```

## Loading Strategy

### Method 1: From first tick (preferred)

KiteConnect ticks include `tick.ohlc.close` which is the previous day's
close. Load it from the first tick received per instrument:

```python
def update_prev_close_from_tick(self, tick) -> None:
    """
    Called for every VALID tick that passes Gate 2.
    Populates prev_close_cache on first tick per instrument.
    """
    token = tick.instrument_token
    if token not in self.prev_close_cache:
        ohlc = getattr(tick, "ohlc", None)
        if ohlc and hasattr(ohlc, "close") and ohlc.close:
            self.prev_close_cache[token] = float(ohlc.close)
```

### Method 2: From Zerodha historical API (fallback)

If the first tick for an instrument doesn't have OHLC data (e.g. LTP mode),
load from Zerodha at startup:

```python
import asyncio

async def load_prev_close_from_zerodha(
    kite,
    instrument_tokens: list[int],
    prev_close_cache: dict[int, float],
) -> None:
    """
    Load previous day close for all watchlist instruments.
    Call once at startup before market open.
    Runs via asyncio.to_thread to avoid blocking event loop.
    """
    import datetime
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()

    for token in instrument_tokens:
        try:
            data = await asyncio.to_thread(
                kite.historical_data,
                token,
                yesterday,
                yesterday,
                "day",
            )
            if data:
                prev_close_cache[token] = float(data[-1]["close"])
        except Exception as e:
            import structlog
            structlog.get_logger().warning(
                "prev_close_load_failed",
                instrument_token=token,
                error=str(e),
            )
            # Failure is non-fatal — Gate 2 will PASS for this instrument
```

## Refresh Policy

**Never refresh during market hours.** prev_close is fixed for the trading
day. NSE circuit breakers are calculated once at day open.

When to refresh:
- At system startup (before first tick arrives)
- After system restart mid-day (startup reconciliation)
- Never on reconnect — the close price hasn't changed

## Gate 2 Interaction

When `prev_close_cache` has no entry for an instrument token:

```python
prev_close = self.prev_close_cache.get(token)
if prev_close is None:
    return True  # PASS gate — no reference data, don't block
```

This is the correct behavior: if we don't know the prev close, we can't
apply the circuit breaker filter, so we let the tick through. It's better
to miss an anomalous tick than to block all ticks for an instrument at startup.

## Weekend / Holiday Handling

If today is Monday, prev_close = Friday's close. The historical API
call automatically handles this — use `date.today() - timedelta(days=1)` and
Zerodha will return the most recent trading day's close.

For Indian market holidays, the same logic applies. Always use the API
result; never hardcode dates.
