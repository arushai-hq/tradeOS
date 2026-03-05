---
name: tradeos-observability
description: >
  TradeOS D4 observability enforcer — structured logging, Telegram alerting,
  and Prometheus metrics for the Indian algo trading system on NSE/BSE.

  Use this skill whenever working on: structlog configuration, JSON log
  schemas for trade/risk/system events, Telegram alerts (which events to send,
  format, rate limiting), Prometheus metrics definitions, log rotation setup,
  phase-aware observability (Phase 1 = structlog+Telegram only, Phase 2 = full
  Prometheus+Grafana stack), or the daily 15:35 IST PnL summary.

  Invoke for tasks like: "add structured logging to order placement",
  "send Telegram when kill switch fires", "log position_closed with all PnL
  fields", "set up observability for TradeOS", "write the daily summary
  Telegram", "add Prometheus metrics for drawdown", "configure structlog with
  IST timestamps", "write log entry for a trade event", "set up Telegram rate
  limiting for warnings", "phase 2 Prometheus registration".

  Do NOT invoke for: general Python logging (not TradeOS), Django/Flask web
  app logging, email notifications, user activity analytics, generic Prometheus
  setup outside TradeOS, or server access logs.
related-skills: monitoring-expert, python-pro, tradeos-kill-switch-guardian, tradeos-websocket-resilience
---

# TradeOS D4 — Observability Stack

TradeOS uses a two-phase observability design. Phase 1 (active, ₹50K) requires
only structlog + Telegram — no external infrastructure. Phase 2 adds Prometheus
+ Grafana when scaling to ₹3L+. Code must be phase-aware: Phase 1 components
never import or require Phase 2 infrastructure.

## Phase Summary

| Phase | When | Stack |
|-------|------|-------|
| Phase 1 | Active now (₹50K) | structlog JSON + Telegram alerts |
| Phase 2 | Scaling (₹3L+) | + Prometheus + Grafana + Loki on VPS |

## Reference Routing

Read the relevant file for your task:

| Task | Read |
|------|------|
| Configure structlog, IST timestamps, log rotation | `references/structlog-configuration.md` |
| Log any trade/risk/system event | `references/event-schemas.md` |
| Send a Telegram alert, rate limiting, formats | `references/telegram-alerting-rules.md` |
| Prometheus metrics, Phase 2 conditional init | `references/prometheus-metrics-phase2.md` |
| Activate Phase 2 without rewriting Phase 1 | `references/phase-migration-guide.md` |

## Core Rules (memorize these)

**Never use `print()` in production code.** Every event goes through structlog.

**Never log sensitive values.** API keys, access tokens, and account numbers
must be masked or omitted — log the error type, not the credential.

**Log schemas are contracts.** Every `position_closed` log across every module
must have the same fields. Inconsistent schemas break log analysis. See
`references/event-schemas.md` for the canonical field list.

**Phase 1 ≠ Phase 2 dependency.** If your code imports `prometheus_client` at
module level, it will crash on a machine without it. Guard with
`OBSERVABILITY_PHASE` env var check or lazy import.

**Telegram is not a firehose.** Only defined event types go to Telegram. WARNING
alerts are batched (max 1 per 5 min per type). CRITICAL alerts bypass batching.
See `references/telegram-alerting-rules.md` for the exact list.

**Async safety.** structlog's JSONRenderer is synchronous but fast — safe for
the hot path. File writes via RotatingFileHandler are synchronous; wrap in
`asyncio.to_thread()` if writing from a hot coroutine path.

## Quick Reference

```python
import structlog
log = structlog.get_logger()

# Trade event (always include all schema fields)
log.info("position_closed",
    symbol="RELIANCE", strategy="s1",
    entry_price=2450.50, exit_price=2487.00,
    pnl_rs=367.50, pnl_pct=1.5,
    hold_duration_minutes=47, exit_reason="target_hit")

# Risk event
log.warning("daily_loss_warning",
    daily_pnl_pct=-2.1, threshold_pct=-2.0, capital_at_risk=10500)

# NEVER do this
log.info("auth_failed", api_key="kite_xxxxx")  # ❌ credential leak
print("order placed")  # ❌ no print in production
```
