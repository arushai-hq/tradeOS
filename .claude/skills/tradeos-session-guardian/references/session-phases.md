# Session Phases — TradeOS D9

## Overview

A TradeOS session transitions through 4 phases from cold start to clean shutdown.
The system is designed around the principle that **each phase failure is terminal for that session** —
the appropriate response is always `sys.exit()` or kill switch escalation, not a retry loop.
Trading capital is at risk; ambiguous startup state is worse than no trading.

---

## Phase Transition Diagram

```
[Cold Start]
     │
     ▼
┌─────────────────────────────────────────────────┐
│  PHASE 0 — Pre-Market Gate                      │
│  Runs synchronously, before asyncio.run()       │
│  6 checks in strict order                       │
│  Any failure → sys.exit(1) [or sys.exit(0)]     │
└─────────────────────────────────────────────────┘
     │ All 6 pass
     │ shared_state["pre_market_gate_passed"] = True
     ▼
┌─────────────────────────────────────────────────┐
│  PHASE 1 — Startup Sequence                     │
│  asyncio.run() begins here                      │
│  11 ordered steps                               │
│  D7 reconciliation BLOCKS if positions ≠ 0     │
└─────────────────────────────────────────────────┘
     │ shared_state["system_ready"] = True
     ▼
┌─────────────────────────────────────────────────┐
│  PHASE 2 — Active Trading (09:15–15:00 IST)     │
│  5 async tasks running concurrently             │
│  Background health monitors active              │
│  Kill switch hierarchy enforced (D1)            │
└─────────────────────────────────────────────────┘
     │ Clock reaches 15:00 IST
     ▼
┌─────────────────────────────────────────────────┐
│  PHASE 3 — EOD Shutdown (15:00–15:30 IST)       │
│  Scheduled, not anomalous                       │
│  Positions closed, reconciliation, summary      │
│  sys.exit(0) at 15:30                           │
└─────────────────────────────────────────────────┘
```

---

## Shared State Keys By Phase

### Set in Phase 0
| Key | Value set | Who reads it |
|-----|-----------|-------------|
| `pre_market_gate_passed` | `True` | Phase 1 startup gate assertion |
| `telegram_active` | `True` / `False` | All alert-sending code |
| `zerodha_user_id` | From `kite.profile()` | Logging, audit |

### Set in Phase 1
| Key | Value set | Who reads it |
|-----|-----------|-------------|
| `session_date` | `today_ist` (YYYY-MM-DD) | Phase 2 date drift monitor, logs |
| `session_start_time` | `datetime.now(IST)` | Heartbeat, daily summary |
| `system_ready` | `True` | signal_processor gate |
| All 19 shared_state keys | Default values | All tasks |

### Modified in Phase 2
All standard D6 shared state keys (see `tradeos-async-architecture` references).
Phase 2 adds these Phase 3 triggers:
| Key | Set by | Meaning |
|-----|--------|---------|
| `accepting_signals` | risk_watchdog at 15:00 | Signals blocked for EOD |

### Set in Phase 3
| Key | Value set | Meaning |
|-----|-----------|---------|
| `system_ready` | `False` | Shutdown in progress |
| `accepting_signals` | `False` | No new entries |

---

## How D1–D8 Relate to Session Phases

| Discipline | Active Phase | Integration point |
|------------|-------------|-------------------|
| D1 Kill Switch | Phase 2, Phase 3 | Never triggered by Phase 3 EOD — scheduled shutdown is not anomalous |
| D2 Order State Machine | Phase 1 startup, Phase 2 | On startup: query all open orders before trading |
| D3 WebSocket | Phase 1 startup, Phase 2 | Connection happens in Phase 1 step 7 |
| D4 Observability | All phases | Phase 0 validates the alert path; Phase 3 sends daily summary |
| D5 Tick Validator | Phase 2 only | Not started until WS connected in Phase 1 |
| D6 Async Architecture | Phase 1, Phase 2 | Tasks started in Phase 1 steps 5–10 |
| D7 Reconciliation | Phase 1 (BLOCKING), Phase 2 | Blocks startup if positions non-zero |
| D8 Test Pyramid | Covers all phases | Tests for pre-market gate, startup, shutdown all required |

---

## The Stale Position Problem

The most critical Phase 1 concern is **stale positions from a prior session**.

If TradeOS crashed mid-session the previous day, it may have live open positions in Zerodha
that were never closed. S1 is MIS intraday — Zerodha will auto-square-off these at 15:20,
but TradeOS won't know about them. If a new session starts without detecting these, TradeOS
may open duplicate positions in the same instruments, doubling exposure unknowingly.

**Prevention:** Phase 1 Step 4 calls D7 reconciliation and checks whether
`shared_state["open_positions"]` is empty after reconciliation. If any positions exist:
- Log CRITICAL with position details
- Send Telegram alert
- `sys.exit(1)` — do not start trading

The operator must manually verify and close stale positions before restarting.

---

## What Cannot Be Retried

These failures are designed to be terminal — retrying would mask a problem that needs human attention:

| Failure | Why not retry |
|---------|--------------|
| Expired Zerodha token | Token refresh requires browser OAuth — not automatable |
| Invalid API credentials | Indicates a configuration error, not a transient fault |
| Stale positions found | Duplicate exposure risk — requires human verification |
| Startup after 12:00 IST | With only 3 hours of trading left, partial-day data risks bad signals |
| WebSocket timeout at startup | Indicates market connectivity problem — not safe to trade |
