# Performance Constraints — TradeOS D5

## The < 1ms Requirement

The tick validator sits in the hot path of the signal processor. KiteConnect
delivers ticks at up to ~1000 ticks/second across 20 instruments. Each tick
must be validated and either forwarded to strategy or discarded. If validation
takes more than 1ms per tick, the queue backs up.

## Why All 5 Gates Are O(1)

| Gate | Operation | Why O(1) |
|------|-----------|---------|
| Gate 1 | `price > 0` | Single comparison, no lookup |
| Gate 2 | `dict.get(token)` + arithmetic | Hash map lookup + float arithmetic |
| Gate 3 | `volume >= 0` | Single comparison |
| Gate 4 | `datetime.now() - timestamp` | System call + subtraction |
| Gate 5 | `dict.get(token)` + 2 comparisons | Hash map lookup + equality checks |

Total: ~5 dict lookups + ~10 arithmetic/comparison operations = **< 10 µs** per tick.

## What Must NEVER Be in the Validator

```python
# ❌ API call — milliseconds to seconds of latency
kite.historical_data(token, ...)

# ❌ Database query — network round-trip
db.query("SELECT close FROM prices WHERE token=?", token)

# ❌ Loop over collections — O(n)
for instrument in all_instruments:
    if instrument.token == tick.token: ...

# ❌ File I/O — blocking syscall
with open("prices.csv") as f:
    data = f.read()

# ❌ JSON parsing — non-trivial CPU
json.loads(raw_tick_string)

# ❌ Network call — always blocking
requests.get("http://api.zerodha.com/...")
```

## What IS Acceptable

```python
# ✓ Dict lookup — O(1), sub-microsecond
prev_close = self.prev_close_cache.get(token)

# ✓ Float arithmetic — nanoseconds
deviation = abs(price - prev_close) / prev_close

# ✓ DateTime subtraction — microseconds
age = (datetime.now(IST) - exchange_ts).total_seconds()

# ✓ Attribute access — nanoseconds
price = tick.last_price
volume = tick.volume_traded

# ✓ Small dict write — O(1)
self.last_tick[token] = {"price": price, "ts": ts}

# ✓ Structlog call — ~5 µs (CPU-bound JSON serialization)
log.warning("stale_tick", ...)
```

## Async Safety

The validator is called from the signal processor coroutine (asyncio event
loop). It must not block the event loop with any I/O. Since all operations
are CPU-bound and take < 1ms, there is no need to wrap in `asyncio.to_thread()`.

```python
async def signal_processor_task(
    tick_queue: asyncio.Queue,
    validator: TickValidator,
) -> None:
    while True:
        ticks = await tick_queue.get()  # only suspend point
        for tick in ticks:
            # validate() is synchronous but fast (<1ms)
            # no await needed — does not block event loop
            if validator.validate(tick):
                await strategy_engine.on_tick(tick)  # strategy is async
```

## Logging Performance Note

`log.warning(...)` via structlog adds ~5-15 µs due to JSON serialization.
For discarded ticks (Gates 1-4), this is acceptable — bad ticks are rare.
For Gate 5 (duplicates), logging is deliberately suppressed because duplicates
can arrive in bursts of hundreds on reconnect — logging each would add
measurable overhead.

## Profiling Approach

If you suspect performance issues:

```python
import time

start = time.perf_counter()
result = validator.validate(tick)
elapsed = time.perf_counter() - start

if elapsed > 0.001:  # > 1ms
    log.warning("slow_tick_validation",
                elapsed_ms=round(elapsed * 1000, 3))
```

This is diagnostic code only — remove from production hot path.
