---
name: tradeos-architecture
description: >
  Understand TradeOS system architecture, module responsibilities, and data flow.
  Use whenever making changes that touch multiple modules or when understanding
  how components interact. Invoke for architecture questions, cross-module changes,
  new feature planning, data flow tracing, or understanding the strategy slot system.
  Do NOT invoke for: single-module changes where the scope is clear, general Python
  architecture patterns, or non-TradeOS projects.
related-skills: tradeos-async-architecture, tradeos-session-guardian, tradeos-kill-switch-guardian
---

# TradeOS Architecture Guide

## Module Map

| Module | Responsibility | Key Files |
|--------|---------------|-----------|
| `data_engine/` | WebSocket feed from Zerodha, tick validation (D5), tick storage to TimescaleDB | `ws_manager.py`, `tick_validator.py`, `tick_storage.py` |
| `strategy_engine/` | CandleBuilder (15-min), technical indicators, S1 signal generator, risk gate checks | `candle_builder.py`, `indicators.py`, `s1_signal_generator.py` |
| `risk_manager/` | Kill switch (D1), position sizer, PnL tracker, daily loss enforcement | `kill_switch.py`, `position_sizer.py`, `pnl_tracker.py` |
| `execution_engine/` | Order state machine (D2), paper/live order placement, order monitoring | `order_state_machine.py`, `paper_broker.py`, `order_monitor.py` |
| `regime_detector/` | 4-regime classifier (trending_up, trending_down, range_bound, volatile) | `regime_detector.py` |
| `paper_trader/` | Paper trade execution and position tracking | `paper_trader.py` |
| `backtester/` | Historical strategy backtesting | `backtester.py` |
| `strategies/` | Strategy definitions (S1-S4 slots) | Strategy implementation files |
| `hawk_engine/` | HAWK AI market intelligence system | See `docs/hawk_spec.md` |
| `utils/` | Telegram notifier, time utilities, DB events | `telegram_notifier.py`, `time_utils.py` |
| `tools/` | Session report, HAWK CLI, DB backfill | `session_report.py`, `hawk_cli.py` |
| `scripts/` | Token cron, token server, log rotation, setup | `token_cron.sh`, `log_rotation.sh` |

## Data Flow (Tick to Trade)

```
Zerodha KiteConnect WebSocket
    │
    ▼
ws_listener task (data_engine/ws_manager.py)
    │  validates via TickValidator (5 gates)
    ▼
tick_queue (asyncio.Queue, maxsize=10000)
    │
    ▼
signal_processor task (strategy_engine/)
    │  CandleBuilder → 15-min candles
    │  Indicators → RSI, VWAP, ATR, volume ratio
    │  S1 signal generator → BUY/SELL signals
    │  Risk gates → kill switch check, max positions, daily loss
    ▼
order_queue (asyncio.Queue, maxsize=100)
    │
    ▼
execution_engine/ → paper_broker or live broker
    │
    ▼
order_monitor task → polls kite.orders() every 5s
    │  updates OrderStateMachine (8 states)
    ▼
PnL tracker → realized + unrealized P&L
    │
    ▼
risk_watchdog task → checks drawdown every 1s
    │  triggers kill switch if thresholds breached
    ▼
heartbeat task → alive log every 30s + Telegram alerts
```

## Strategy Slot System

TradeOS supports 4 strategy slots with configurable capital allocation:

| Slot | Strategy | Allocation | Status |
|------|----------|-----------|--------|
| S1 | Intraday Momentum | 70% (₹7,00,000) | Active — Phase 1 |
| S2 | (Reserved) | 15% | Future |
| S3 | (Reserved) | 10% | Future |
| S4 | (Reserved) | 5% | Future |

**Allocation Sum Rule:** All allocations MUST sum to 1.00 (validated at startup).

## S1 Intraday Momentum — Key Parameters

- **Candle interval:** 15 minutes
- **Instruments:** NIFTY50 constituents (from `config/settings.yaml`)
- **Entry signals:** RSI + VWAP + volume ratio + regime filter
- **Risk per trade:** 1.5% of slot capital
- **Max open positions:** 4
- **Hard exit:** 15:00 IST (no new entries after `no_entry_after` config, default 14:45)
- **Stop loss:** ATR-based, minimum 2% floor

## Position Lifecycle

```
Signal Generated
    │
    ▼
Risk Gates (kill switch → max positions → daily loss → position sizer)
    │
    ▼
Order Placed (PENDING → OPEN → PLACED)
    │
    ▼
Order Filled (PLACED → FILLED) — broker confirms
    │
    ▼
Position Open (tracked in shared_state["positions"])
    │  monitored by risk_watchdog (1s) + heartbeat (30s)
    ▼
Exit Trigger (stop_hit | target_hit | hard_exit | kill_switch)
    │
    ▼
Exit Order → Position Closed → PnL recorded
```

## Shared State Contract

The `shared_state` dict is the central coordination point for all 5 async tasks.
See the `tradeos-async-architecture` skill for ownership rules and field specifications.

## Entry Points

| Entry Point | Purpose |
|-------------|---------|
| `bin/tradeos` | Production CLI (bash shim) — ALWAYS use this |
| `main.py` | D9 session lifecycle (called by tradeos CLI) |
| `python -m pytest tests/` | Test runner (or `tradeos test`) |

## Cross-Module Dependencies

- `strategy_engine` depends on `data_engine` (ticks), `risk_manager` (gates), `regime_detector` (regime)
- `execution_engine` depends on `risk_manager` (kill switch check before every order)
- `risk_manager` is independent (no circular deps) — everything checks risk, risk checks nothing
- `main.py` orchestrates all modules via D9 session lifecycle
