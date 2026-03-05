---
name: tradeos-session-guardian
description: |
  TradeOS D9 session lifecycle enforcer — the mandatory reference for all TradeOS
  startup, shutdown, and mid-session health code. You MUST invoke this skill any time
  the task involves: Zerodha token freshness check at startup, NSE holiday or weekend
  detection, kite.profile() live API probe, IST time window validation (wait before
  08:45 / warn for 09:10-12:00 / abort after 12:00), Telegram startup probe, blocking
  startup on stale prior-session positions, the 15:00 IST hard exit (set
  accepting_signals=False, drain tick_queue — do NOT trigger kill switch), EOD
  shutdown sequence (15:20 reconciliation, 15:25 daily summary, 15:30 system stop),
  mid-session kite.profile() token health monitor (every 60 min, Level 3 on 401/403,
  no refresh attempt), or session_date drift detection.

  This skill covers the complete pre-market gate (6 checks in sequence), the full
  11-step startup that blocks on D7 reconciliation, all mid-session monitors, and
  the Phase 3 EOD shutdown. If any of those words appear in the task — pre-market,
  token check, holiday, Telegram at startup, stale positions, 15:00 hard exit,
  end-of-day shutdown, session date — invoke this skill immediately.

  Do NOT invoke for: WebSocket reconnect backoff (D3), kill switch trigger conditions
  (D1), position reconciliation comparison algorithm (D7), daily P&L and Prometheus
  metrics (D4), tick validation 5-gate pipeline (D5), order state machine (D2).
related-skills: python-pro, tradeos-kill-switch-guardian, tradeos-websocket-resilience, tradeos-position-reconciler, tradeos-observability, tradeos-async-architecture, tradeos-order-state-machine
---

# TradeOS Session Guardian — D9

A TradeOS session has **four phases** that run in strict sequential order.
A failure in any phase triggers `sys.exit()` or the appropriate kill switch level —
never a retry loop. The previous phase must complete successfully before the next begins.

## Phase Map

| Phase | Name | Window | Entry Gate |
|-------|------|--------|------------|
| 0 | Pre-Market Gate | Before any network connection | All 6 checks pass |
| 1 | Startup Sequence | After gate passes | D7 reconciliation = all positions zero |
| 2 | Active Trading | 09:15–15:00 IST | `shared_state["system_ready"] = True` |
| 3 | EOD Shutdown | 15:00–15:30 IST | Hard exit triggered at exactly 15:00 |

## Reference Files

Read the file for the phase you are implementing:

| File | When to read |
|------|-------------|
| `references/session-phases.md` | Full 4-phase overview, cross-phase state, how D1–D8 relate |
| `references/phase0-premarket-gate.md` | Phase 0: all 6 checks with Python implementations |
| `references/phase1-startup-sequence.md` | Phase 1: 11-step startup sequence |
| `references/phase2-active-monitors.md` | Phase 2: 3 mid-session health monitors |
| `references/phase3-eod-shutdown.md` | Phase 3: 15:00–15:30 EOD shutdown sequence |
| `references/nse-holidays-maintenance.md` | How to maintain `config/nse_holidays.yaml` |

## Phase 0 Quick Reference

| Check | Hard stop trigger | Exit type |
|-------|------------------|-----------|
| CHECK 1: All required config/secret keys present | Any key missing | `sys.exit(1)` |
| CHECK 2: `token_date` matches today IST | Stale token | `sys.exit(1)` + Telegram |
| CHECK 3: `kite.profile()` succeeds | API error / 401 | `sys.exit(1)` + Telegram |
| CHECK 4: NSE holiday or weekend | Holiday/Sat/Sun | `sys.exit(0)` + Telegram |
| CHECK 5: Telegram test message | Failure → non-blocking | `telegram_active = False`, continue |
| CHECK 6: IST time window | > 12:00 IST | `sys.exit(1)` |

All 6 pass → `shared_state["pre_market_gate_passed"] = True`
→ Telegram: `"🟢 TradeOS {date}: Pre-market gate passed. Starting up."`

## Phase 0 Entry Point

```python
def run_pre_market_gate(shared_state: dict) -> KiteConnect:
    """
    Runs all 6 Phase 0 checks in strict sequential order.
    Returns a validated KiteConnect instance on success.
    Calls sys.exit() on any hard-stop condition — never raises or retries.
    See references/phase0-premarket-gate.md for full implementation.
    """
```

Called from `main.py` before `asyncio.run()`:
```python
if __name__ == "__main__":
    shared_state = _init_shared_state()
    kite = run_pre_market_gate(shared_state)   # synchronous, before event loop
    asyncio.run(main(kite, shared_state))
```

## Key Invariants

- `token_date` in `secrets.yaml` must match today IST — Zerodha OAuth requires browser, no auto-refresh
- `nse_holidays.yaml` must cover the current year — update in December (see maintenance guide)
- Telegram failure is **non-blocking** — trading continues with file-only alerts prefixed `[TELEGRAM_FAILED]`
- Prior session positions **block startup** — D7 must confirm all positions zero before `system_ready = True`
- All time comparisons use `pytz.timezone("Asia/Kolkata")` — never `datetime.now()` without IST
- Phase 3 shutdown (15:00) is a **scheduled sequence** — not a kill switch event, not anomalous
