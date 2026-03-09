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

**Current Phase: PAPER TRADING SESSION 02 — Session 01 complete, regime detector added**
- Session 01 ran 6hr (2026-03-06): 129k ticks, 340 candles, 0 signals (VWAP bug fixed)
- Two bugs fixed post Session 01: `average_price` → `average_traded_price` + EOD CancelledError
- Regime detector module built: 4-regime classifier (BULL/BEAR/HIGH_VOL/CRASH) + RiskGate Gate 7
- venv is now required — run `source activate.sh` before any python command
- docker-compose.yml moved to `docker/` — use `bash scripts/db_start.sh`
- Next: Session 02 Monday — first signal evaluation with VWAP fix + regime gating active

**Capital:** ₹5L total. ₹50K for Phase 1 live. Paper trade until proven.
**Exchange:** NSE Equities only. No F&O until Phase 3.

---

## Repo Structure

```
tradeOS/
├── START.md                          ← YOU ARE HERE
├── README.md                         ← Project overview + quick start
├── main.py                           ← Entry point — D9 session lifecycle
├── schema.sql                        ← TimescaleDB schema (5 tables)
├── requirements.txt
├── config/
│   ├── settings.yaml                 ← Capital, risk rules, watchlist
│   ├── secrets.yaml.template         ← API key template (never commit secrets)
│   └── nse_holidays.yaml             ← NSE trading calendar 2025–2026
├── data_engine/                      ← KiteConnect WebSocket + TickValidator + storage
├── strategy_engine/                  ← CandleBuilder, IndicatorEngine, S1SignalGenerator
├── risk_manager/                     ← PositionSizer, LossTracker, PnlTracker
├── execution_engine/                 ← OrderStateMachine (8 states), paper orders
├── regime_detector/                  ← 4-regime market classifier (Nifty 50 + VIX)
├── utils/                            ← time_utils, telegram, db_events
├── strategies/
│   ├── s1_intraday/                  ← ELIMINATED — S1 logic in strategy_engine/
│   ├── s2_swing/                     ← Phase 2
│   ├── s3_positional/                ← Phase 3
│   └── s4_event/                     ← Phase 3
├── backtester/                       ← backtesting.py integration (Phase 2)
├── paper_trader/                     ← Paper mode simulator (Phase 2)
├── docker/                           ← Docker infrastructure
│   ├── docker-compose.yml            ← TimescaleDB service definition
│   └── README.md                     ← DB commands, volume, port binding notes
├── scripts/
│   ├── setup.sh                      ← One-time venv setup (Mac + Rocky Linux)
│   ├── refresh_token.py              ← Daily Zerodha OAuth token refresh (~90s)
│   ├── verify_token.py               ← Quick token validity check
│   ├── test_telegram.py              ← Standalone Telegram connection test
│   ├── db_start.sh                   ← Start TimescaleDB container
│   ├── db_stop.sh                    ← Stop container (data preserved)
│   ├── db_migrate.sh                 ← Apply schema.sql
│   └── README.md                     ← Daily workflow docs
├── tests/                            ← 211 unit + integration tests
├── logs/                             ← Structured JSON logs (gitignored)
└── docs/
    ├── strategy_specs/
    │   ├── S1_intraday_momentum.md   ← Full S1 spec — read before coding S1
    │   └── regime_detector_spec.md   ← Regime detector design rationale + thresholds
    ├── brainstorm/
    │   ├── session_001_architecture.md
    │   └── session_002_research_findings.md
    └── diagrams/
        ├── reliability/              ← 8 reliability diagrams — READ BEFORE CODING
        │   └── README.md             ← Full discipline reference (D1–D8)
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
Skill Phase: COMPLETE ✅
  All 9 discipline skills built (D1–D9) + 5 architecture fixes applied.
  Audit passed (6/6 verification checks).

Phase 1 Components:
  data_engine/          ✅ COMPLETE — commit 5772779
    WebSocket feed, TickValidator (5-gate), storage, schema.sql
    Tests: 60 unit + 11 integration (skipped without DB_DSN) — all pass

  strategy_engine/      ✅ COMPLETE — commit 5d811e5
    CandleBuilder, IndicatorEngine, S1SignalGenerator, RiskGate, WarmupManager
    Tests: 50 unit — all pass. mypy clean.

  risk_manager/         ✅ COMPLETE — commit 52a2ffa
    PositionSizer, ChargeCalculator, PnlTracker, LossTracker, RiskManager
    Tests: 41 unit + 3 integration (skipped without DB_DSN) — all pass. mypy clean.
    Total: 112 tests pass across all 3 modules (11 skipped — need DB_DSN).

  execution_engine/     ✅ COMPLETE — commit ce4434a
    OrderStateMachine (8 states), OrderPlacer, OrderMonitor, ExitManager, ExecutionEngine
    Startup reconciliation (D2), paper mode gate, kill switch gate (D1)
    Tests: 26 unit — all pass. mypy clean.

  regime_detector/      ✅ COMPLETE — commit a26eb38..dd51d77
    MarketRegime enum (BULL_TREND, BEAR_TREND, HIGH_VOLATILITY, CRASH)
    classify_regime() pure function — strict priority (CRASH > HIGH_VOL > BEAR > BULL)
    RegimeDetector class — kite.historical_data() REST, 60s refresh, D3 resilience
    RiskGate Gate 7 — blocks counter-trend signals + CRASH volume gate
    Integrated: StrategyEngine, main.py Phase 1 init, risk_watchdog 60s refresh
    Tests: 25 unit (classification, signal gates, resilience, validation) + 4 Gate 7 tests
    Docs: regime_detector/README.md + docs/strategy_specs/regime_detector_spec.md

  main.py               ✅ COMPLETE — commit c34dee1
    Single entry point. D9 session lifecycle (Phase 0–3).
    Phase 0: 6 pre-market checks (token, holiday, Telegram, time window)
    Phase 1: async startup — engines in dependency order
    Phase 2: 5 D6 concurrent tasks (ws_listener, signal_processor, order_monitor,
             risk_watchdog, heartbeat)
    Phase 3: EOD shutdown at 15:30 IST
    utils/: time_utils, telegram, db_events
    config/nse_holidays.yaml: 2025-2026 NSE calendar
    Tests: 36 new tests (211 total w/ regime_detector, 12 skipped DB_DSN). mypy clean.

  strategies/s1_intraday/ ⚪ ELIMINATED — S1 logic absorbed into strategy_engine/signal_generator.py
                           (lines 119-176). No separate module needed. Not built, not needed.

  backtester/           🔴 Pending — after paper trading validates live behaviour
  paper_trader/         🔴 Pending — after paper trading validates live behaviour

Current Phase: PAPER TRADING ← NOW
  Session 01 (2026-03-06): 6hr uptime, 129k ticks, 340 candles, 0 signals.
  Bugs fixed: average_price→average_traded_price (VWAP), CancelledError on EOD.
  Session 02: Monday — first real signal evaluation with fixes active.

Bug Fixes (Session 01):
  BUG-1 FIXED: candle_builder.py used tick.get("average_price") — always None
    in pykiteconnect v5. Field is average_traded_price. vwap=close for all
    candles → S1 conditions never met → 0 signals. Fixed commit 820b2bc.
  BUG-2 FIXED: asyncio.CancelledError not caught on EOD shutdown → traceback
    + exit code 1. Added except CancelledError in main() gather + __main__
    block → clean sys.exit(0) at 15:30 IST. Fixed commit a48bf6e.

Docs & Specs:
  docs/strategy_specs/S1_intraday_momentum.md   ✅ Complete
  docs/strategy_specs/regime_detector_spec.md    ✅ Complete
  docs/diagrams/reliability/ (D0–D8)             ✅ Complete
  docs/brainstorm/session_001_architecture.md    ✅ Complete
  docs/brainstorm/session_002_research_findings.md ✅ Complete
  config/settings.yaml                           ✅ Complete

Skills (.claude/skills/):
  tradeos-kill-switch-guardian  ✅ D1 — 3-level kill switch hierarchy + is_trading_allowed gate
  tradeos-order-state-machine   ✅ D2
  tradeos-websocket-resilience  ✅ D3
  tradeos-observability         ✅ D4 — structlog + Telegram rate-limiting + Prometheus Phase 2
  tradeos-tick-validator        ✅ D5
  tradeos-async-architecture    ✅ D6 — 5-task definitions + shared state contract
  tradeos-position-reconciler   ✅ D7 — uses kite.positions()["day"] (MIS-only fix applied)
  tradeos-test-pyramid          ✅ D8
  tradeos-session-guardian      ✅ D9 — pre-market gate (6 checks) + startup + mid-session + EOD shutdown
```

---

## Pending Implementation TODOs (Architecture Review — Code Phase)

These gaps were identified during architecture review and deferred to the code phase.
Each must be handled when building the relevant module — do not skip.

| # | Gap | Module | Details |
|---|-----|--------|---------|
| T1 | **Instrument subscription** | `data_engine/` | Before starting WebSocket, call `kite.subscribe(instrument_tokens)` and `kite.set_mode(kite.MODE_FULL, tokens)` for all watchlist instruments. Subscription must happen after token validation and before the WS `on_ticks` callback is registered. Tokens fetched via `kite.instruments("NSE")` filtered to watchlist symbols. |
| T2 | **Partial fill + Level 2 interaction** | `risk_manager/` | When Level 2 fires and a position is in `PARTIALLY_FILLED` state, the close-out order must account for the partial fill quantity (not the original order quantity). `order_monitor` must pass current filled qty to the Level 2 close routine — not stale order qty. |
| T3 | **consecutive_losses counter reset** | `risk_manager/` | Counter resets to 0 on a winning fill. Also resets at session start (midnight IST or first tick of trading day). Never carries over across days. Implementation: `order_monitor` decrements on FILLED-win detection; `risk_watchdog` resets at start-of-day. |
| T4 | **Hard exit at 15:00 IST** | `risk_manager/` | `risk_watchdog` must check `datetime.now(IST).time() >= time(15, 0)` on each 1s cycle. On crossing 15:00: close all open positions via market order (same as Level 2 position close), set `stop_new_signals = True`, do NOT trigger kill switch (this is scheduled, not anomalous). Log INFO `hard_exit_triggered`. |

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
# One-time environment setup (creates .venv)
bash scripts/setup.sh

# Activate venv (required before any python command)
source activate.sh

# Start database (docker-compose.yml is in docker/)
bash scripts/db_start.sh
bash scripts/db_migrate.sh   # apply schema

# Daily token refresh (browser + 2FA, ~90s)
python scripts/refresh_token.py

# Run TradeOS
python main.py

# Run tests
pytest tests/ -v

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
*Last updated: Session 10 (2026-03-06) — Regime detector module built. 4-regime classifier + RiskGate Gate 7 + 29 tests.*
*Next milestone: Paper Session 02 — first signal evaluation with VWAP fix + regime gating active*
