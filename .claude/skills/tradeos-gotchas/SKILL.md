---
name: tradeos-gotchas
description: >
  Critical bug patterns and hard-won lessons from TradeOS development.
  ALWAYS consult before touching position accounting, P&L calculations,
  or order processing. Invoke for position changes, P&L code, order fills,
  exit processing, field name questions, shared_state access, or Telegram
  message formatting. Do NOT invoke for: general Python debugging, non-TradeOS
  projects, or changes that don't touch trading logic.
related-skills: tradeos-architecture, tradeos-kill-switch-guardian, tradeos-position-reconciler, tradeos-order-state-machine
---

# TradeOS Gotchas — Hard-Won Lessons

> **Rule:** Every bug listed here has a corresponding test that prevents recurrence.
> Before touching any of these areas, read the relevant test first.

## Critical Field Names — NEVER Substitute

```python
# CORRECT field names (from Zerodha / shared_state):
position["avg_price"]    # NOT "entry_price"
position["side"]         # NOT "direction"  — values: "BUY" or "SELL"
position["qty"]          # Negative for SHORT positions

# WRONG — these will compile but produce phantom P&L:
position["entry_price"]  # ❌ Does not exist — returns None/KeyError
position["direction"]    # ❌ Does not exist — returns None/KeyError
```

**Why this matters:** B7 — Using `entry_price` instead of `avg_price` produced a phantom
unrealized P&L of -₹199,679, which false-triggered the kill switch 30 seconds into Session 04.

## SHORT Position Accounting

```python
# SHORT positions use NEGATIVE quantity
qty = -100  # This is a SHORT of 100 shares

# Unrealized P&L formula:
unrealized = qty * (avg_price - current_price)
# For SHORT (qty negative): profit when price drops

# ALWAYS check qty sign before any P&L calculation
```

## Bug Catalogue (B1-B14)

### B1 — Hard exit doesn't close positions (CRITICAL)
- **Symptom:** `hard_exit_triggered` at 15:00 but open positions remain
- **Root cause:** No `emergency_exit_all` mechanism existed
- **Fix:** `risk_watchdog` calls `emergency_exit_all` on hard exit (commit `9ca7502`)
- **Guard:** Test verifies emergency exit closes all open positions at 15:00

### B2 — Signals generated after hard exit (CRITICAL)
- **Symptom:** New signals accepted after 15:00 hard exit
- **Root cause:** No halt gate in signal processing path
- **Fix:** `accepting_signals=False` halt gate in `strategy_engine._process_tick` (commit `9ca7502`)
- **Guard:** Test verifies signals rejected when `accepting_signals=False`

### B3 — SHORT signals on oversold RSI (HIGH)
- **Symptom:** SHORT signals generated when RSI ~30 (oversold = should not short)
- **Root cause:** RSI filter checked `30 ≤ rsi ≤ 45` instead of `rsi ≥ 45`
- **Fix:** Corrected RSI range for SHORT signals (commit `f65f8af`)

### B4 — daily_pnl_pct stuck at 0.0 (HIGH)
- **Symptom:** Heartbeat always reports 0% daily P&L
- **Root cause:** `shared_state["last_tick_prices"]` never populated
- **Fix:** Validated ticks populate last_tick_prices; heartbeat computes realized+unrealized (commit `f0a1cf1`)

### B5 — Missing lifecycle logging in paper mode (HIGH)
- **Symptom:** No visibility into signal→order→fill→close lifecycle
- **Fix:** Added 7 lifecycle events: signal_accepted, signal_rejected, order_placed, order_filled, stop_hit, target_hit, position_closed (commit `ca7ddc9`)

### B6 — Queue overflow exceptions (MEDIUM)
- **Symptom:** `Queue.put_nowait` raises `QueueFull` at ~15:44
- **Fix:** `_safe_enqueue()` wraps with `QueueFull` catch, overflow warning logged once (commit `be16168`)

### B7 — Unrealized P&L field mismatch (CRITICAL)
- **Symptom:** Phantom -₹199,679 unrealized loss → false kill switch at 30s
- **Root cause:** Used `entry_price` (doesn't exist) instead of `avg_price`, and `direction` instead of `side`
- **Fix:** Corrected field names + added no-tick guard (commit `cc9c018`)
- **THE canonical example of why field names matter**

### B8 — Ghost LONG positions from exit fills (CRITICAL)
- **Symptom:** After position closed, a new LONG position appears in shared_state
- **Root cause:** `_on_exit_fill` read position data AFTER `on_close` deleted it, creating a new entry
- **Fix:** Snapshot position data BEFORE `on_close` deletes it (commit `7ed6b7a`)
- **Guard:** Test verifies no ghost positions after exit fill processing

### B9 — Session report duplicates (MEDIUM)
- **Symptom:** Report shows duplicate signals/trades
- **Fix:** Parser deduplicates within 5s window, filters ghost entries (entry_price=0, qty=0) (commit `028995d`)

### B10 — Pre-market warning spam (LOW)
- **Symptom:** 94 warnings before 09:15 (nifty_intraday_unavailable, vix_data_unavailable, etc.)
- **Fix:** Downgraded to DEBUG before 9:15 via `is_market_hours()` gate (commit `028995d`)

### B11 — Regime detector double-initialization (LOW)
- **Symptom:** `regime_initialized` logged twice at startup
- **Fix:** `_initialized` guard prevents double-init (commit `028995d`)

### B12 — gross_pnl=0.0 on position close (CRITICAL)
- **Symptom:** All closed positions report zero P&L
- **Root cause:** `emergency_exit_all` used entry_price as exit_price
- **Fix:** Uses tick price for exit (commit `af8a007`)

### B13 — Telegram heartbeat wrong entry/direction (HIGH)
- **Symptom:** Telegram shows wrong entry price and direction
- **Root cause:** Read from shared_state which uses `avg_price`/`side`, not `entry_price`/`direction`
- **Fix:** Uses `resolve_position_fields()` (commit `af8a007`)

### B14 — exit_reason=KILL_SWITCH instead of HARD_EXIT_1500 (MEDIUM)
- **Symptom:** Hard exit at 15:00 logged as kill switch trigger
- **Fix:** `emergency_exit_all` accepts `exit_type` parameter (commit `af8a007`)

## Patterns to Watch

1. **Any code that reads from `shared_state["positions"]`** — check field names against the canonical list above
2. **Any code that calculates P&L** — verify SHORT qty is negative, use `avg_price` not `entry_price`
3. **Any code that processes order fills** — snapshot data before deletion, verify no ghost creation
4. **Any code that touches 15:00 hard exit** — use `accepting_signals=False` + drain, NOT kill_switch.trigger
5. **Any Telegram formatting** — use `resolve_position_fields()` for display values
6. **Any startup initialization** — add `_initialized` guard to prevent double-init
