# Zerodha Tick Payload — TradeOS D5

## KiteConnect Tick Object Fields

KiteConnect delivers ticks as objects with these relevant fields:

```python
tick.instrument_token: int       # Unique instrument ID (e.g. 408065 = RELIANCE)
tick.tradingsymbol: str          # Symbol string (may not always be present)
tick.last_price: float           # Last traded price — Gate 1 + Gate 2
tick.volume_traded: int          # Cumulative day volume — Gate 3
tick.exchange_timestamp: datetime # Exchange tick time — Gate 4
tick.ohlc.open: float            # Day open
tick.ohlc.high: float            # Day high
tick.ohlc.low: float             # Day low
tick.ohlc.close: float           # PREVIOUS DAY close — use for Gate 2
```

## Subscription Mode Availability

Field availability depends on which mode you subscribed in:

| Field | MODE_FULL | MODE_QUOTE | MODE_LTP |
|-------|-----------|------------|---------|
| instrument_token | ✓ | ✓ | ✓ |
| last_price | ✓ | ✓ | ✓ |
| volume_traded | ✓ | ✓ | ✗ |
| exchange_timestamp | ✓ | ✓ | ✗ |
| ohlc.close | ✓ | ✓ | ✗ |

**TradeOS Phase 1 uses MODE_QUOTE** — all validation fields available.

If a field is missing (e.g. MODE_LTP subscription):
- `getattr(tick, "volume_traded", None)` returns `None`
- Gate 3 treats `None` as invalid → DISCARD

## Accessing Fields Safely

Always use `getattr` with a default for optional fields:

```python
# Safe field access
price = getattr(tick, "last_price", None)      # None if missing
volume = getattr(tick, "volume_traded", None)  # None if missing
ts = getattr(tick, "exchange_timestamp", None) # None if missing

# OHLC is a nested object
ohlc = getattr(tick, "ohlc", None)
prev_close = ohlc.close if ohlc and hasattr(ohlc, "close") else None
```

## exchange_timestamp Details

The `exchange_timestamp` is a `datetime` object in the tick. Zerodha
sometimes delivers it as a **naive datetime** (no timezone info). Always
localize before comparing:

```python
import pytz
IST = pytz.timezone("Asia/Kolkata")

ts = tick.exchange_timestamp
if ts is not None and ts.tzinfo is None:
    ts = IST.localize(ts)
```

## instrument_token as Primary Key

All caches (prev_close_cache, last_tick) use `instrument_token` as the key,
not `tradingsymbol`. Token is always present; symbol may be absent:

```python
# Always use token as key
self.prev_close_cache[tick.instrument_token] = prev_close
self.last_tick[tick.instrument_token] = {...}
```

## Common Values for Testing

```python
# RELIANCE NSE
instrument_token = 408065
tradingsymbol = "RELIANCE"
typical_price_range = (2300.0, 3500.0)

# INFY NSE
instrument_token = 408065
tradingsymbol = "INFY"
typical_price_range = (1400.0, 2000.0)
```

## What a Real Tick Looks Like

```python
# Simulated tick object for testing
class MockTick:
    instrument_token = 408065
    tradingsymbol = "RELIANCE"
    last_price = 2451.50
    volume_traded = 1234567
    exchange_timestamp = datetime(2026, 3, 5, 11, 23, 45,
                                   tzinfo=pytz.timezone("Asia/Kolkata"))

    class ohlc:
        open = 2440.0
        high = 2465.0
        low = 2435.0
        close = 2420.0  # previous day close
```
