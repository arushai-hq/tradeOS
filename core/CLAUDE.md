# core/ — TradeOS Trading Engine

Trading engine modules: feed processing, candle building, strategy signals, order execution, risk management.

## Skills

| Skill | When to use |
|-------|-------------|
| tradeos-architecture | System architecture, module map, data flow |
| tradeos-gotchas | Bug patterns (B1-B14), field name traps, P&L pitfalls |
| tradeos-kill-switch-guardian | D1: 3-level kill switch implementation |
| tradeos-order-state-machine | D2: 8-state order lifecycle |
| tradeos-websocket-resilience | D3: Auto-reconnect with exponential backoff |
| tradeos-observability | D4: structlog + Telegram + Prometheus |
| tradeos-tick-validator | D5: 5-gate tick validation pipeline |
| tradeos-async-architecture | D6: 5-task asyncio event loop |
| tradeos-position-reconciler | D7: Zerodha position reconciliation |
| tradeos-session-guardian | D9: Session lifecycle management |

## Commands

```bash
python -m pytest tests/          # Run all tests
python main.py                   # Start engine (from project root)
```

## Conventions

- `structlog` for all logging — no bare `print()`
- `asyncpg` for database, `pykiteconnect` for broker
- Negative qty for SHORT positions
- Use `avg_price` not `entry_price`, `side` not `direction`
- All time operations use `pytz.timezone("Asia/Kolkata")`

## Gotchas

- SHORT position field names differ from LONG (B7 bug pattern)
- Kill switch false positives from unrealized P&L miscalculation (B7-B8)
- `resolve_position_fields()` utility eliminates field name bugs
