---
name: tradeos-websocket-resilience
description: TradeOS D3 WebSocket resilience enforcer for the data_engine module. Use this skill whenever implementing KiteConnect WebSocket reconnection logic, exponential backoff for tick feed disconnects, stale signal detection after reconnect, heartbeat monitoring for silent disconnects, the KiteTicker-to-asyncio thread bridge, or the 60-second Level 2 kill switch trigger on WebSocket timeout. Invoke for tasks like: "write the reconnect loop with exponential backoff", "KiteTicker disconnected — write the handler", "detect stale signals after reconnect", "write heartbeat monitor for tick feed", "bridge KiteTicker thread to asyncio queue", "handle silent WebSocket disconnect", "stop reconnecting after market close", "on_ticks callback for KiteTicker", "ws went down for 60 seconds trigger kill switch", "re-subscribe instruments after reconnect". This skill encodes the exact backoff sequence (2→4→8→16→30s cap), the 5-minute stale signal rule, the IST market hours check (09:15–15:30), and the ws_connected shared state keys that the base model and websocket-engineer skill do NOT know without it. Do NOT invoke for generic WebSocket chat apps, Socket.IO room management, REST API endpoints, database connections, or WebSocket implementations unrelated to Zerodha KiteTicker tick feed resilience.
related-skills: websocket-engineer, python-pro, tradeos-kill-switch-guardian, tradeos-order-state-machine
---

# TradeOS WebSocket Resilience (D3)

The Zerodha KiteConnect WebSocket **will** disconnect. Silence it, recover from it, and never act on stale data. This skill enforces the D3 discipline — the rules that prevent a reconnect from turning into a misfire.

## Connection States

```
DISCONNECTED → CONNECTING → CONNECTED → DISCONNECTED (on drop)
```

**On CONNECTED:** start heartbeat monitor, reset reconnect_attempt=0, re-subscribe watchlist, set ws_connected=True.
**On DISCONNECTED (drop):** set ws_connected=False, record disconnect_timestamp, start reconnect loop immediately.

## Backoff Sequence (non-negotiable)

| Attempt | Wait | Extra action |
|---------|------|--------------|
| 1 | 2s | — |
| 2 | 4s | — |
| 3 | 8s | — |
| 4 | 16s | — |
| 5+ | 30s | Telegram alert every attempt |

Use `asyncio.sleep()` — never `time.sleep()`. Reset counter to 0 on success.

## Stale Signal Rule (most commonly violated)

```python
signal_age = datetime.now(IST) - signal.generated_at
if signal_age > timedelta(minutes=5):
    # DEAD — discard, log WARNING, do NOT pass to strategy
else:
    # LIVE — pass through normally
```

Re-fetch current price for any pending signals before evaluating post-reconnect.

## Shared State Keys (must match D1 + D6)

```python
ws_state = {
    "ws_connected": bool,
    "last_tick_timestamp": datetime | None,
    "disconnect_timestamp": datetime | None,
    "reconnect_attempt": int,
    "instruments_subscribed": list[str],
}
```

## Reference Files

| File | When to read |
|------|-------------|
| `references/kiteticker-integration.md` | KiteTicker callbacks, thread bridge, subscribe modes |
| `references/reconnect-backoff-patterns.md` | Exact backoff loop, market hours guard, Telegram alert |
| `references/stale-signal-detection.md` | 5-min rule, IST timezone, post-reconnect price check |
| `references/heartbeat-monitor.md` | Silent disconnect detection, 30s task |
| `references/shared-state-contract.md` | All shared state keys, D1/D6 integration |

## What This Skill Prevents

- Processing ticks inside `on_ticks` callback (blocks KiteTicker thread)
- Using `time.sleep()` in reconnect loop (blocks asyncio event loop)
- Acting on signals older than 5 minutes after reconnect
- Stopping reconnect attempts during market hours
- Missing the 60s → Level 2 kill switch trigger on disconnect
- Silent disconnect not caught because TCP stayed open
- Re-subscribing to wrong instrument list after reconnect
