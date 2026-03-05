# Event Schemas — TradeOS D4

These are the canonical field definitions for every significant TradeOS event.
All fields are required — missing fields are a defect. Consistent schemas
enable log analysis, alerting, and future Loki/Grafana queries.

## Table of Contents
- [Trade Events](#trade-events)
- [Risk Events](#risk-events)
- [System Events](#system-events)
- [Usage Pattern](#usage-pattern)

---

## Trade Events

### signal_generated
Fired when S1 (or any strategy) generates a new trade signal.

```python
log.info("signal_generated",
    symbol="RELIANCE",
    strategy="s1",
    direction="long",          # "long" | "short"
    entry_price=2450.50,
    stop_loss=2426.25,         # entry - 1% (or strategy-defined)
    target=2523.00,            # entry + 2.5R or strategy-defined
    indicators={
        "ema9": 2448.30,
        "ema21": 2441.50,
        "rsi": 58.4,
        "vwap": 2445.00,
        "volume_ratio": 1.8,   # current_vol / avg_vol
    },
)
```

### order_placed
Fired immediately after `kite.place_order()` returns an order_id.

```python
log.info("order_placed",
    order_id="241014000000001",
    symbol="RELIANCE",
    strategy="s1",
    direction="long",
    qty=10,
    price=2450.50,
    order_type="LIMIT",        # "LIMIT" | "MARKET" | "SL" | "SL-M"
    kill_switch_level=0,       # current kill switch level at time of placement
)
```

### order_filled
Fired when Order State Machine transitions to FILLED.

```python
log.info("order_filled",
    order_id="241014000000001",
    symbol="RELIANCE",
    qty=10,
    fill_price=2451.00,
    fill_timestamp="2026-03-05T11:23:45+05:30",
    slippage_pct=0.02,         # (fill_price - order_price) / order_price * 100
)
```

### order_rejected
Fired when Order State Machine transitions to REJECTED.

```python
log.warning("order_rejected",
    order_id="241014000000001",
    symbol="RELIANCE",
    reason="insufficient_funds",   # Zerodha rejection reason string
    consecutive_rejections=1,      # from RiskManager counter
)
```

### position_closed
Fired when a position is fully exited. This is the primary P&L record.

```python
log.info("position_closed",
    symbol="RELIANCE",
    strategy="s1",
    entry_price=2451.00,
    exit_price=2487.50,
    pnl_rs=365.00,             # (exit - entry) * qty, in INR
    pnl_pct=1.49,              # pnl_rs / (entry * qty) * 100
    hold_duration_minutes=47,
    exit_reason="target_hit",  # "target_hit" | "stop_loss" | "hard_exit_time"
                               # | "kill_switch" | "manual"
)
```

---

## Risk Events

### kill_switch_triggered
CRITICAL event — also triggers Telegram alert.

```python
log.critical("kill_switch_triggered",
    level=2,                   # 1 | 2 | 3
    reason="daily_loss_exceeded",  # see D1 trigger list
    daily_pnl_pct=-3.1,
    consecutive_losses=2,
    positions_open=2,
    action_taken="cancel_all_orders",  # what Level 2 did
)
```

### daily_loss_warning
WARNING event at 2% drawdown (fires before the 3% kill switch).

```python
log.warning("daily_loss_warning",
    daily_pnl_pct=-2.1,
    threshold_pct=-2.0,        # the warning threshold (not kill switch)
    capital_at_risk=10500,     # current open position value in INR
)
```

### position_mismatch
CRITICAL — fires during reconciliation when local != broker qty.

```python
log.critical("position_mismatch",
    symbol="RELIANCE",
    local_qty=10,
    broker_qty=0,              # what Zerodha reports
    action_taken="lock_instrument",
)
```

---

## System Events

### system_start
Fired once at startup after reconciliation completes successfully.

```python
log.info("system_start",
    mode="paper",              # "paper" | "live"
    capital_total=500000,      # from settings.yaml
    strategies_active=["s1"],
    instruments_count=20,      # len(watchlist)
    phase=1,                   # observability phase
)
```

### ws_disconnected
Fired when KiteTicker `on_close` or `on_error` fires.

```python
log.warning("ws_disconnected",
    disconnect_timestamp="2026-03-05T11:20:00+05:30",
    reconnect_attempt=1,       # attempt number (1-indexed)
    market_hours=True,
)
```

### ws_reconnected
Fired when KiteTicker `on_connect` fires after a disconnect.

```python
log.info("ws_reconnected",
    downtime_seconds=8.3,
    reconnect_attempt=2,
    signals_discarded=0,       # stale signals discarded during downtime
)
```

### reconciliation_complete
Fired after each reconciliation run (startup + every 30 min).

```python
log.info("reconciliation_complete",
    positions_checked=3,
    mismatches_found=0,
    locks_applied=0,
)
```

---

## Usage Pattern

```python
import structlog
log = structlog.get_logger()

# Always use keyword arguments — never positional
log.info("event_name", field1=value1, field2=value2)

# Level guide:
# log.debug()    → detailed diagnostics, not production
# log.info()     → normal operation events (orders, fills, signals)
# log.warning()  → abnormal but recoverable (ws disconnect, high drawdown)
# log.error()    → recoverable errors (API call failed, retry will happen)
# log.critical() → immediate human attention required (kill switch, mismatch)
```

## Field Naming Conventions

- **Timestamps**: ISO 8601 strings (`"2026-03-05T11:23:45+05:30"`), not floats
- **Prices**: Always floats, in INR (e.g., `2450.50`)
- **PnL**: `_rs` suffix for rupee amounts, `_pct` suffix for percentages
- **Duration**: `_seconds` or `_minutes` suffix
- **Counts**: bare integer field (e.g., `consecutive_rejections=3`)
- **Booleans**: snake_case (e.g., `market_hours=True`)
