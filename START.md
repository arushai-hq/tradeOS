# START.md — TradeOS Claude Code Session Bootstrap
> Read this file first. Every time. Before writing a single line of code.

---

## Who You Are Working With

**Builder:** Irfan — Founder of Arushai Systems Private Limited, Doha, Qatar.
**Project:** TradeOS — AI-powered systematic trading system for Indian markets (NSE).
**Broker:** Zerodha KiteConnect API.
**Language:** Python. Comfort level: solid.
**Goal:** Build a fault-tolerant, paper-trade-first, then live automated trading system.

---

## What This Project Is

TradeOS is a 4-layer automated trading pipeline:

```
Data Engine  →  Strategy Engine  →  Risk Manager  →  Execution Engine
(KiteConnect)   (S1/S2/S3/S4)       (Kill Switch)    (Zerodha orders)
```

**Current Phase: Phase 1 — Active**
- Build Data Engine (KiteConnect WebSocket + REST)
- Build S1 Intraday Momentum strategy
- Run in paper mode only
- Backtest S1 on 1yr historical data
- Deploy ₹50K live ONLY after all testing gates pass

**Capital:** ₹5L total. ₹50K for Phase 1 live. Paper trade until proven.
**Exchange:** NSE Equities only. No F&O until Phase 3.

---

## Repo Structure

```
tradeOS/
├── START.md                          ← YOU ARE HERE
├── README.md                         ← Project overview
├── config/
│   ├── settings.yaml                 ← Capital, risk rules, watchlist
│   └── secrets.yaml.template         ← API key template (never commit secrets)
├── data_engine/                      ← KiteConnect WebSocket + historical data
├── strategies/
│   ├── s1_intraday/                  ← PHASE 1 ACTIVE
│   ├── s2_swing/                     ← Phase 2
│   ├── s3_positional/                ← Phase 3
│   └── s4_event/                     ← Phase 3
├── risk_manager/                     ← Kill switch + position sizing
├── execution_engine/                 ← Order placement via KiteConnect
├── backtester/                       ← backtesting.py integration
├── paper_trader/                     ← Paper mode simulator
├── logs/                             ← Structured JSON logs
└── docs/
    ├── strategy_specs/
    │   └── S1_intraday_momentum.md   ← Full S1 spec — read before coding S1
    ├── brainstorm/
    │   ├── session_001_architecture.md
    │   └── session_002_research_findings.md
    └── diagrams/
        ├── reliability/              ← 8 reliability diagrams — READ BEFORE CODING
        │   ├── README.md             ← Full discipline reference
        │   ├── D0_master_overview.excalidraw
        │   ├── D1_kill_switch_hierarchy.excalidraw
        │   ├── D2_order_state_machine.excalidraw
        │   ├── D3_websocket_resilience.excalidraw
        │   ├── D4_observability_stack.excalidraw
        │   ├── D5_data_validation.excalidraw
        │   ├── D6_async_architecture.excalidraw
        │   ├── D7_position_reconciliation.excalidraw
        │   └── D8_testing_pyramid.excalidraw
        ├── 01_system_overview.excalidraw
        ├── 02_build_roadmap.excalidraw
        └── 03_s1_strategy_logic.excalidraw
```

---

## Confirmed Technology Stack

| Layer | Tool | Notes |
|-------|------|-------|
| Data (live) | `pykiteconnect` WebSocket | Official Zerodha library |
| Data (historical) | `pykiteconnect` REST API | For backtesting |
| Indicators | `pandas-ta` | 150+ indicators, zero compilation |
| Backtesting | `backtesting.py` | Phase 1 validation |
| Execution | `pykiteconnect` REST + `kite-mcp-server` | Code + Claude native |
| Async | `asyncio` | Event-driven, non-blocking |
| Logging | `structlog` (JSON) | Phase 1. Prometheus + Grafana in Phase 2 |
| State Machine | `python-statemachine` | For order lifecycle |
| Circuit Breaker | `pybreaker` | Kill switch implementation |
| Alerts | Telegram Bot | Critical events only |
| Testing | `pytest` | Unit + integration |
| Data validation | Custom `TickValidator` | 5-gate filter |

**Install all at once:**
```bash
pip install kiteconnect pandas pandas-ta backtesting pybreaker python-statemachine structlog pytest
```

---

## The 8 Non-Negotiable Reliability Disciplines

These are NOT optional. Read `docs/diagrams/reliability/README.md` for full detail.
Every component you build must comply with these:

### D1 — Kill Switch (3 Levels)
```
Level 1: Stop new signals. Positions stay.    [trigger: 3 losses / daily loss > 3%]
Level 2: Cancel orders. Close positions.       [trigger: WS down > 60s / API errors > 5]
Level 3: Kill everything.                      [trigger: manual / position mismatch]
```
Tool: `pybreaker`

### D2 — Order State Machine (8 States)
```
CREATED → SUBMITTED → ACKNOWLEDGED → PARTIALLY_FILLED → FILLED
                                   → REJECTED
                                   → PENDING_CANCEL → CANCELLED
                                   → EXPIRED / PENDING_UPDATE
```
**On every restart:** query Zerodha open orders BEFORE placing anything.
Tool: `python-statemachine`

### D3 — WebSocket Resilience
- Auto-reconnect with exponential backoff: 2s → 4s → 8s → 16s → 30s cap
- Stale signal detection: signal age > 5 min after reconnect = discard
- Heartbeat: no tick in 30s = trigger reconnect

### D4 — Observability
- Phase 1: `structlog` JSON logs + Telegram alerts
- Phase 2: Prometheus + Grafana + Loki on VPS
- Every trade produces: `{ts, level, event, order_id, strategy, pnl, error}`

### D5 — Data Validation (5 Gates)
Every tick must pass before touching strategy logic:
1. `price > 0`
2. `price within ±20% of previous close`
3. `volume >= 0`
4. `timestamp within last 5 seconds`
5. `not duplicate of previous tick`
**Rule:** Never halt on bad tick. Discard, log, continue.

### D6 — Async Architecture (5 Tasks)
```python
# All running concurrently in one asyncio event loop
Task 1: WebSocket Listener    (tick → queue)
Task 2: Signal Processor      (queue → strategy)
Task 3: Order Monitor         (poll Zerodha every 5s)
Task 4: Risk Watchdog         (drawdown check every 1s)
Task 5: Heartbeat             (system alive every 30s)
```
**Rule:** Any blocking I/O must use `asyncio.to_thread()`. Never block the event loop.

### D7 — Position Reconciliation
- Runs at: startup, every 30 min, after any disruption
- Mismatch detected → LOCK instrument → LOG → Telegram alert
- **Zerodha is source of truth. Always.**

### D8 — Testing Pyramid (3 Layers)
```
Layer 1 Unit Tests (pytest):        TickValidator, RiskManager, KillSwitch, StateMachine
Layer 2 Integration (paper mode):   P&L accuracy, reconciliation, WS reconnect
Layer 3 Simulation (backtesting):   S1 on 1yr data, drawdown sim, Monte Carlo
```
**Live deployment gate:** All 3 layers pass → ₹50K goes live.

---

## S1 Strategy — Quick Reference

Full spec: `docs/strategy_specs/S1_intraday_momentum.md`

**Entry (Long):** 9 EMA crosses above 21 EMA + Volume > 1.5x avg + Price above VWAP + RSI 55–70
**Entry (Short):** 9 EMA crosses below 21 EMA + Volume > 1.5x avg + Price below VWAP + RSI 30–45
**Exit:** Stop = prev swing low/high | Target = 1:2 RR | Hard exit = 15:00 IST
**Universe:** 20 NIFTY 50 stocks (see `config/settings.yaml` watchlist)
**Risk per trade:** 1.5% of S1 capital = ₹2,250 max loss

---

## Risk Rules (Hardcoded — Never Bypass)

```yaml
# From config/settings.yaml
max_loss_per_trade_pct: 1.5%    # of allocated strategy capital
max_daily_loss_pct:     3.0%    # of total capital → triggers kill switch
max_open_positions:     3       # simultaneously across all strategies
hard_exit_time:         15:00   # IST — no intraday positions after this
stop_loss:              MANDATORY on every order. No stop = no trade.
```

---

## Secrets — Never Commit

```bash
# API keys go here — this file is gitignored
cp config/secrets.yaml.template config/secrets.yaml
# Then edit secrets.yaml with your Zerodha api_key and api_secret
```

Zerodha KiteConnect docs: https://kite.trade/docs/connect/v3/
Kite MCP server (Claude native): https://mcp.kite.trade/mcp

---

## Current Build Status

```
Phase 1 Components:
  data_engine/          🔴 Empty — BUILD THIS FIRST
  risk_manager/         🔴 Empty — BUILD SECOND (D1 Kill Switch + D7 Reconcile)
  strategies/s1_intraday/ 🔴 Empty — BUILD THIRD
  backtester/           🔴 Empty — BUILD FOURTH
  paper_trader/         🔴 Empty — BUILD FIFTH
  execution_engine/     🔴 Empty — BUILD LAST (only after paper trade passes)

Docs & Specs:
  docs/strategy_specs/S1_intraday_momentum.md   ✅ Complete
  docs/diagrams/reliability/ (D0–D8)             ✅ Complete
  docs/brainstorm/session_001_architecture.md    ✅ Complete
  docs/brainstorm/session_002_research_findings.md ✅ Complete
  config/settings.yaml                           ✅ Complete
```

---

## How to Work With Me in This Session

1. **Tell me what to build next** — I will read the relevant spec/diagram first, then code
2. **I will always follow the 8 reliability disciplines** — do not let me skip them
3. **Paper mode is default** — `config/settings.yaml` → `mode: paper`. Never change to `live` until all 3 test layers pass
4. **One component at a time** — finish and test before moving to next
5. **Every file I create gets a corresponding pytest unit test**

---

## Quick Commands

```bash
# Install dependencies
pip install kiteconnect pandas pandas-ta backtesting pybreaker python-statemachine structlog pytest

# Run tests
pytest tests/ -v

# Start paper trading (once built)
python main.py --mode paper

# Check logs
tail -f logs/tradeos.log | python -m json.tool
```

---

## Context From Previous Sessions

All strategic decisions were made in Claude.ai web UI and documented here:
- **Architecture:** `docs/brainstorm/session_001_architecture.md`
- **Research findings (tools, frameworks):** `docs/brainstorm/session_002_research_findings.md`
- **Reliability engineering:** `docs/diagrams/reliability/README.md`
- **S1 full spec:** `docs/strategy_specs/S1_intraday_momentum.md`

The Claude.ai web UI session has persistent memory. This file is the bridge to bring that context into Claude Code terminal sessions.

---

*TradeOS — Arushai Systems Private Limited*
*Last updated: Session 3 — Reliability Engineering Design Phase*
*Next milestone: Data Engine build*
