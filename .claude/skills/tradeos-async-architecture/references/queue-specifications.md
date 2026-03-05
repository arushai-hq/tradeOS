# Queue Specifications — TradeOS D6

## Overview

Phase 1 uses exactly 2 queues. Never add more.

| Queue | Capacity | Producer | Consumer |
|-------|----------|----------|----------|
| `tick_queue` | 1000 ticks | `ws_listener` | `signal_processor` |
| `order_queue` | 100 orders | `signal_processor` | paper_trader / execution_engine |

Queues are created at startup and stored in `shared_state` so heartbeat can report queue depths.

```python
tick_queue  = asyncio.Queue(maxsize=1000)
order_queue = asyncio.Queue(maxsize=100)
shared_state["tick_queue"]  = tick_queue
shared_state["order_queue"] = order_queue
```

---

## tick_queue

**Purpose:** Bridge between ws_listener (tick validation) and signal_processor (strategy logic).

**Overflow policy — drop oldest, never block producer:**

```python
async def put_tick_safe(queue: asyncio.Queue, tick) -> None:
    """Non-blocking put. If queue is full, drop oldest tick to make room."""
    if queue.full():
        try:
            queue.get_nowait()  # discard oldest
            log.warning("queue_overflow",
                        queue="tick_queue",
                        depth=queue.maxsize)
        except asyncio.QueueEmpty:
            pass
    await queue.put(tick)
```

**Why this policy?** The ws_listener callback runs in a thread bridge (`call_soon_threadsafe`).
If it blocks on `queue.put()`, it blocks the KiteConnect callback thread and causes tick loss
across ALL instruments. Dropping one old tick is always better than blocking the producer.

**Consumer pattern:**
```python
async def signal_processor_fn(shared_state: dict) -> None:
    tick_queue = shared_state["tick_queue"]
    while True:
        tick = await tick_queue.get()  # only suspend point
        try:
            _process_tick(tick, shared_state)
        finally:
            tick_queue.task_done()
```

---

## order_queue

**Purpose:** Decouple signal generation from order placement.
`signal_processor` produces signals; `paper_trader` or `execution_engine` consumes them.

**Overflow policy — reject signal, never block:**

```python
async def put_order_safe(queue: asyncio.Queue, signal: dict) -> None:
    """Non-blocking put. If queue is full, reject signal and log."""
    if queue.full():
        log.warning("order_queue_full",
                    queue="order_queue",
                    depth=queue.maxsize,
                    signal_symbol=signal.get("symbol"))
        return  # signal dropped — do not block signal_processor
    await queue.put(signal)
```

**Why 100-item limit?** An order queue backing up to 100 signals means the execution
engine is ~100 orders behind. At that point, signals are too stale to trade safely.
Better to discard than to place outdated orders.

**Consumer pattern:**
```python
async def paper_trader_fn(shared_state: dict) -> None:
    order_queue = shared_state["order_queue"]
    while True:
        signal = await order_queue.get()
        try:
            await process_signal(signal, shared_state)
        finally:
            order_queue.task_done()
```

---

## Queue Depth Monitoring

Heartbeat reports queue depths every 30 seconds.
Alert if tick_queue > 500 (50% full) or order_queue > 50 (50% full):

```python
tick_depth  = shared_state["tick_queue"].qsize()
order_depth = shared_state["order_queue"].qsize()

if tick_depth > 500:
    log.warning("tick_queue_high_watermark", depth=tick_depth, capacity=1000)

if order_depth > 50:
    log.warning("order_queue_high_watermark", depth=order_depth, capacity=100)
```

---

## What Is NOT a Queue

Tasks do NOT use queues for:
- Sharing risk state (use shared_state dict)
- Communicating kill switch level (use kill_switch object + shared_state)
- Passing order fill notifications (use shared_state["open_orders"] polling)
- Sending Telegram alerts (call send_telegram directly, no queue)
