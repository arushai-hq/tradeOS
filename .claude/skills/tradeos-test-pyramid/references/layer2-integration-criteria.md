# Layer 2 — Integration Test Gate Criteria

## What Layer 2 Means

Layer 2 is not an automated test that runs in CI. It's a structured 3-week paper trading observation period where the system runs with `mode: paper` in `config/settings.yaml` against live NSE market data — but places no real orders.

The purpose is to verify that the system behaves correctly across a full range of real market conditions: normal sessions, volatile days, gap-ups, expiry days, early disconnects. These scenarios are hard to simulate in unit tests.

## Duration Requirement

**Minimum 3 calendar weeks** of continuous paper trading. Not 3 weeks of uptime — 3 weeks of market calendar. The observation window must include at least 12 trading sessions.

Starting Layer 3 simulation before Layer 2 completes is not permitted even if all 5 criteria look green partway through.

## The 5 Criteria (All Must Pass Simultaneously)

### Criterion 1 — Zero Manual Interventions

The system ran without a human having to manually fix state.

Specifically:
- No direct edits to `shared_state` outside normal code paths
- Reconciliation ran cleanly every 30-minute scheduled cycle
- No instruments stayed locked beyond one market session
- No Telegram alert required a human action to unblock trading

Why this matters: A system that requires daily babysitting will fail catastrophically during a fast market move. If you're watching it, you're not trading optimally.

### Criterion 2 — Kill Switch Accuracy

Every kill switch trigger was justified by the actual condition, not noise.

Specifically:
- Zero false positives during normal market operation
- Every trigger that fired was caused by a real threshold breach
- Kill switch fired within **2 seconds** of the threshold breach being detected (measured from log timestamps)
- No Level 2 or Level 3 fires that didn't meet their stated criteria

### Criterion 3 — Order Lifecycle Integrity

Every order placed in paper mode reached a terminal state.

Terminal states: FILLED, CANCELLED, REJECTED, EXPIRED

Specifically:
- 100% of orders resolved within the same trading session
- Zero orders in non-terminal state at end-of-day (EOD 15:30)
- On every reconciliation run, the order state machine state matched Zerodha's reported state
- PARTIALLY_FILLED orders were never counted as complete

### Criterion 4 — WebSocket Stability

The WebSocket reconnect mechanism worked without human intervention.

Specifically:
- Every disconnect triggered the exponential backoff sequence and eventually reconnected
- 100% reconnect success rate (0 permanent failures)
- No stale signals (age > 5 minutes) were processed after any reconnect
- Heartbeat correctly detected every silent disconnect (no tick for 30s) and triggered reconnect

### Criterion 5 — P&L Accounting Accuracy

The system's P&L tracking matched reality.

Specifically:
- Paper trade P&L vs manual calculation: within **±0.1%** error
- Slippage estimate tracked vs actual simulated fill prices
- Daily Telegram summary at 15:35 IST matched the actual session data exactly
- No P&L drift across the 3-week period (errors shouldn't compound)

## How to Track These Criteria

Maintain a Layer 2 observation log. For each trading session, record:

```
Date: 2024-01-15
Sessions: 6.25 hours (09:15 – 15:30)
Kill switch triggers: 0
WS disconnects: 1 (reconnected in 12s)
Orders placed: 4 | Resolved: 4 | Stuck: 0
Manual interventions: 0
P&L paper: -₹1,240 | Manual check: -₹1,238 | Delta: 0.016% ✓
Notes: Volatile session post-FOMC. System handled 4 sequential signal rejections cleanly.
```

If any criterion fails during the 3-week window, the clock does NOT reset. Record the failure, investigate root cause, fix the bug, and continue observation. Only reset the 3-week clock if a code change was made that could affect the failed criterion.

## Layer 2 is not optional

The purpose of Layer 2 is to surface integration bugs that don't appear in unit tests. Classic examples:

- Unit tests mock Zerodha, so order state bugs only appear against real API responses
- Unit tests freeze time, so 15:00 IST hard exit bugs only appear in real market sessions
- Unit tests don't simulate real WebSocket traffic patterns, so reconnect edge cases emerge only in live paper trading
- Unit tests test components in isolation; Layer 2 tests their interaction under real timing conditions
