# CLAUDE.md — TradeOS Operating Instructions
> This file is auto-read by Claude Code on every session start.
> Do not modify without syncing with the Web UI brainstorm session.

---

## Project Identity

**TradeOS** — AI-powered systematic trading system, Indian markets (NSE/BSE).
**Owner:** Irfan — Arushai Systems Private Limited, Doha, Qatar.
**Stack:** Python | Zerodha KiteConnect | asyncio | pandas-ta | backtesting.py
**Current Phase:** Phase 1 — Data Engine + S1 Intraday Momentum + Paper Trade only.

---

## Read These Files First

Before writing any code, read the relevant context file for the task:

| What you're building | Read this first |
|----------------------|----------------|
| Anything | `START.md` — full project state + build queue |
| Any component | `docs/diagrams/reliability/README.md` — 8 disciplines |
| Data Engine | `docs/brainstorm/session_002_research_findings.md` |
| S1 Strategy | `docs/strategy_specs/S1_intraday_momentum.md` |
| Risk / Kill Switch | `docs/diagrams/reliability/README.md` → D1 + D7 |
| Config / secrets | `config/settings.yaml` + `config/secrets.yaml.template` |
| Architecture | `docs/brainstorm/session_001_architecture.md` |

---

## How This Project Is Run

**Two-role workflow:**
- **Claude.ai Web UI** → brainstorming, architecture, decisions, diagram generation
- **Claude Code (you)** → receives precise prompts from Web UI, writes code, runs tests, pushes to GitHub

You will receive prompts from the Web UI session that look like:
```
## CONTEXT / ## TASK / ## RULES / ## ACCEPTANCE CRITERIA / ## OUTPUT
```
Execute them exactly as specified. Do not invent scope.

---

## Non-Negotiable Rules

### 1. Mode Safety
```yaml
# config/settings.yaml must always be:
mode: paper    # NEVER change to 'live' without explicit instruction
```
Do not write any code that switches `mode: live` automatically.
Live deployment is a manual, deliberate act after all test gates pass.

### 2. Secrets
- Never write API keys, tokens, or passwords in any file
- Secrets go in `config/secrets.yaml` which is gitignored
- Use `config/secrets.yaml.template` as the reference template
- Load secrets via: `os.environ` or `yaml.safe_load(open('config/secrets.yaml'))`

### 3. Every Component Must Implement These (Non-Negotiable)

**D1 — Kill Switch:** Every order path must check kill switch state before executing.
Use `pybreaker`. Three levels: Trade Stop → Position Stop → System Stop.
Triggers: 3 consecutive losses | daily loss > 3% | WS down > 60s | API errors > 5/5min.

**D2 — Order State Machine:** Never treat order as FILLED until broker confirms.
Track all 8 states. On restart: query Zerodha open orders before placing anything.
Use `python-statemachine`.

**D3 — WebSocket Resilience:** Auto-reconnect with exponential backoff (2→4→8→16→30s cap).
Discard signals older than 5 min after reconnect. Heartbeat: 30s.

**D5 — Data Validation:** Every tick through 5-gate `TickValidator` before strategy logic.
Gates: price > 0 | within ±20% close | volume >= 0 | timestamp < 5s | not duplicate.
Never halt on bad tick — discard, log, continue.

**D6 — Async Architecture:** All I/O is non-blocking.
Use `asyncio.to_thread()` for any blocking call. Five concurrent tasks minimum.

**D7 — Reconciliation:** Run at startup + every 30 min + after any disruption.
Mismatch → lock instrument → log → Telegram alert. Zerodha is always source of truth.

### 4. Logging
Every significant event must produce a structured JSON log line:
```python
log.info("order_placed", order_id=order_id, strategy="s1", symbol=symbol, qty=qty)
log.error("kill_switch_triggered", level=2, reason="daily_loss_exceeded", pnl=pnl)
```
Use `structlog`. Output to `logs/tradeos.log`. No bare `print()` statements in production code.

### 5. Tests Are Mandatory
Every new module gets a corresponding test file:
```
data_engine/websocket_listener.py  →  tests/test_websocket_listener.py
risk_manager/kill_switch.py        →  tests/test_kill_switch.py
```
Minimum test coverage per module:
- Happy path works
- Kill switch blocks when active
- Bad input is rejected gracefully
- No test = code is not done

### 6. Risk Rules Are Hardcoded Constants
```python
# Never accept these as runtime parameters — they are constants
MAX_LOSS_PER_TRADE_PCT = 0.015   # 1.5%
MAX_DAILY_LOSS_PCT     = 0.030   # 3.0%
MAX_OPEN_POSITIONS     = 3
HARD_EXIT_TIME         = "15:00"  # IST
```

---

## Folder Conventions

```
data_engine/          WebSocket listener, tick validation, historical data fetch
risk_manager/         Kill switch, position sizing, reconciliation, drawdown tracking
strategies/s1_intraday/  Signal generation, entry/exit logic for S1
strategies/s2_swing/     (Phase 2 — do not touch)
backtester/           backtesting.py integration, historical run harness
paper_trader/         Paper mode order simulator
execution_engine/     Live order placement via pykiteconnect (Phase 1 last)
tests/                All pytest test files — mirror the module structure
logs/                 Log output — gitignored, never commit log files
config/               settings.yaml (safe to commit) + secrets.yaml (gitignored)
docs/                 Specs, diagrams, brainstorm notes — read-only from code
```

---

## Git Commit Convention

```
feat: add TickValidator with 5-gate filter
feat: implement kill switch Level 1 and Level 2
feat: websocket listener with exponential backoff reconnect
test: add unit tests for RiskManager daily loss trigger
fix: order state machine not handling PARTIALLY_FILLED
docs: update START.md build status after data_engine complete
refactor: extract VWAP calculation to indicators module
```

Always push to `main` on task completion unless told otherwise.

---

## Build Order (Do Not Skip Ahead)

```
1. data_engine/          ← CURRENT — build first
2. risk_manager/         ← Kill Switch (D1) + Reconciliation (D7)
3. strategies/s1_intraday/
4. backtester/
5. paper_trader/
6. execution_engine/     ← Last — only after paper test gates pass
```

---

## Key External References

- Zerodha KiteConnect docs: https://kite.trade/docs/connect/v3/
- Kite MCP server (official): https://github.com/zerodha/kite-mcp-server
- Hosted MCP endpoint: `https://mcp.kite.trade/mcp`
- pykiteconnect: `pip install kiteconnect`
- pandas-ta docs: https://github.com/twopirllc/pandas-ta
- backtesting.py docs: https://kernc.github.io/backtesting.py/

---

## What NOT to Do

- Do not write blocking I/O in the asyncio event loop
- Do not place live orders in paper mode
- Do not commit `config/secrets.yaml`
- Do not commit files in `logs/`
- Do not skip writing tests for a module
- Do not change `mode: live` in settings.yaml
- Do not build Phase 2+ components until Phase 1 is tested and green
- Do not use bare `except:` — always catch specific exceptions and log them
- Do not use `time.sleep()` in async code — use `await asyncio.sleep()`
- Do not invent risk parameters — use constants from `config/settings.yaml`

---

## Git Branching Rules

- main = production. Always deployable. Runs on VPS.
- feature/* = new features (e.g., feature/hawk, feature/trailing-stop)
- fix/* = bug fixes (e.g., fix/pnl-short-direction)
- Create feature branches from main: `git checkout -b feature/name main`
- Keep in sync: `git rebase main` regularly
- Merge to main only when ALL tests pass and feature is complete
- Never commit new feature code directly to main
- Track active branches:
  - main: S1 trading engine (production)
  - (future) feature/hawk: AI watchlist engine

---

*Arushai Systems Private Limited — TradeOS*
*Web UI brainstorm session → Claude Code execution pipeline*
*Context last updated: Session 3 — Post reliability engineering design*
