# Blocking I/O Rules — TradeOS D6

## The Golden Rule

**Any operation that could take > 1ms MUST use `asyncio.to_thread()`.**
A blocking call in the event loop freezes ALL 5 tasks until it returns.

---

## Mandatory asyncio.to_thread() Calls

These Zerodha API calls MUST be wrapped:

```python
# ✅ kite.orders() — network round-trip, typically 50-200ms
orders = await asyncio.to_thread(kite.orders)

# ✅ kite.place_order() — network round-trip
order_id = await asyncio.to_thread(
    kite.place_order,
    tradingsymbol="RELIANCE",
    exchange="NSE",
    transaction_type="BUY",
    quantity=10,
    product="MIS",
    order_type="MARKET",
    variety="regular",
)

# ✅ kite.positions() — network round-trip
positions = await asyncio.to_thread(kite.positions)

# ✅ kite.historical_data() — can take 200ms-2s
data = await asyncio.to_thread(
    kite.historical_data,
    instrument_token=256265,
    from_date=yesterday,
    to_date=today,
    interval="day",
)

# ✅ kite.cancel_order() — network round-trip
await asyncio.to_thread(kite.cancel_order, variety="regular", order_id=order_id)

# ✅ kite.margins() — network round-trip
margins = await asyncio.to_thread(kite.margins)
```

**Pattern for any blocking call:**
```python
result = await asyncio.to_thread(blocking_function, *args, **kwargs)
```

---

## Safe in the Event Loop (No Wrapping Needed)

These operations are non-blocking and safe to call directly:

```python
# ✅ Dict operations — Python GIL-protected, nanoseconds
shared_state["ws_connected"] = True
pnl = shared_state["daily_pnl_pct"]

# ✅ asyncio.Queue operations — sub-microsecond
await tick_queue.put(tick)
tick = await tick_queue.get()
depth = tick_queue.qsize()

# ✅ Arithmetic and comparisons — nanoseconds
deviation = abs(price - prev_close) / prev_close

# ✅ structlog JSON logging — ~5-15µs (CPU-bound, not I/O)
log.info("signal_generated", symbol="RELIANCE", ...)

# ✅ datetime operations — microseconds
age = (datetime.now(IST) - tick.exchange_timestamp).total_seconds()

# ✅ httpx async HTTP (Telegram) — already async, no wrapping
async with httpx.AsyncClient() as client:
    await client.post(url, json=payload)
```

---

## Banned in the Event Loop

```python
# ❌ time.sleep() — blocks entire event loop
time.sleep(5)
# ✅ Use: await asyncio.sleep(5)

# ❌ requests.get() — synchronous blocking HTTP
requests.get("https://api.telegram.org/...")
# ✅ Use: httpx.AsyncClient (already async)

# ❌ Any Zerodha API call without to_thread
kite.orders()          # blocks for 50-200ms
kite.place_order(...)  # blocks for 50-200ms
# ✅ Use: await asyncio.to_thread(kite.orders)

# ❌ Blocking file operations
with open("log.txt", "w") as f:
    f.write(...)  # blocks on I/O
# ✅ structlog writes are thread-safe and fast; for heavy writes use asyncio.to_thread

# ❌ json.loads() on large payloads — CPU-bound, > 1ms for large responses
data = json.loads(large_response)  # problematic at scale
# ✅ For large responses: await asyncio.to_thread(json.loads, large_response)
```

---

## Error Handling in to_thread Calls

Always catch exceptions from to_thread calls:

```python
try:
    orders = await asyncio.to_thread(kite.orders)
except Exception as e:
    log.error("order_monitor_api_error", error=str(e))
    # Continue polling — do not crash order_monitor
```

Never let a single API failure crash the monitoring loop.

---

## KiteConnect Thread Bridge

KiteConnect WebSocket runs in its own thread (not asyncio).
The `on_ticks` callback is called from that thread and must NOT do asyncio operations directly.

**Correct pattern:**
```python
def on_ticks_callback(ws, ticks):
    """Called from KiteConnect thread — bridge to asyncio."""
    loop.call_soon_threadsafe(
        lambda: asyncio.ensure_future(
            ws_listener_receive_ticks(ticks, shared_state)
        )
    )

async def ws_listener_receive_ticks(ticks: list, shared_state: dict) -> None:
    """Runs in the asyncio event loop — safe to do asyncio operations."""
    tick_queue = shared_state["tick_queue"]
    for tick in ticks:
        if validator.validate(tick):
            await put_tick_safe(tick_queue, tick)
```

**Why `call_soon_threadsafe`?** Direct asyncio calls from a non-asyncio thread will crash
with `RuntimeError: no running event loop`. `call_soon_threadsafe` safely schedules
a callback to run in the event loop from an external thread.
