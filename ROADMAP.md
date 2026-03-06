# TradeOS Roadmap

## V1.0 — Foundation (Building Now)

Goal: Prove the system works. Paper trade 3 weeks. Deploy ₹50K live.

### Skills (Complete)
- [x] D1 Kill Switch Guardian
- [x] D2 Order State Machine
- [x] D3 WebSocket Resilience
- [x] D4 Observability Stack
- [x] D5 Tick Validator
- [x] D6 Async Architecture
- [x] D7 Position Reconciler
- [x] D8 Trading Test Pyramid
- [x] D9 Session Guardian

### Modules
- [x] Data Engine — tick feed, validation, TimescaleDB storage
- [x] Strategy Engine — 15-min candles, EMA/RSI/VWAP, S1 signal generation
- [x] Risk Manager — position sizing, P&L tracking, charge calculation
- [x] Execution Engine — order placement, fill monitoring, exit management
- [x] Integration — main.py wiring all engines together
- [ ] Paper trading — 3 weeks minimum, all D8 Layer 2 criteria must pass
- [ ] Live deployment — ₹50K, S1 strategy only

### Strategy
- S1: Intraday Momentum (15-min, NIFTY 50 universe, MIS)
- Entry: EMA9/21 cross + VWAP filter + RSI 55-70 (long) / 30-45 (short) + volume 1.5x
- Risk: 1.5% per trade, max 3 positions, 3% daily stop

---

## V2.0 — Intelligence Layer (After V1.0 is Proven Live)

> Do NOT build any of this until V1.0 has run live for at least 4 weeks
> with consistent performance. V2.0 adds complexity — V1.0 must be
> stable and profitable before any of this is introduced.

### 2.1 — LLM Pre-Market Brief (Claude API)

**What it does:**
Every morning at 07:00 IST, Claude reads market context and sets
the day's trading parameters before the session starts.

**Claude reads:**
- Yesterday's TradeOS performance (from TimescaleDB)
- Market news and sentiment (web search)
- Global cues: SGX Nifty, US futures overnight
- NSE FII/DII data from previous session
- Corporate events today (results, dividends, ex-dates)
- Nifty 50 technical context (weekly/monthly trend)

**Claude outputs (structured JSON → config override for today):**
```json
{
  "run_today": true,
  "avoid_instruments": ["HDFC", "RELIANCE"],
  "avoid_reasons": {"HDFC": "Q3 results today", "RELIANCE": "ex-dividend"},
  "rsi_long_range": [60, 75],
  "rsi_short_range": [25, 40],
  "risk_per_trade_pct": 0.01,
  "max_positions": 2,
  "brief": "Trending day expected post RBI policy. Reduce short bias."
}
```

**Architecture:**
- New module: `pre_market/claude_brief.py`
- Runs as part of D9 Session Guardian Phase 0 (after token check, before market open)
- Uses Claude API (claude-sonnet-4-x) with web_search tool enabled
- Output stored in DB (system_events table, event_type=CLAUDE_BRIEF)
- If Claude API fails → use V1.0 defaults (never block trading on API failure)
- Requires: ANTHROPIC_API_KEY in secrets.yaml

**Key principle:**
Claude sets PARAMETERS. Python executes with those parameters.
Claude never places orders. Claude never overrides kill switch.
The reliability layer (D1-D9) always takes precedence.

---

### 2.2 — Zerodha MCP Integration (kite-mcp-server)

**What it does:**
Allows Claude (in pre-market brief) to READ live account state
directly via MCP — positions, P&L, order history, margins.

**Use cases:**
- Claude sees actual open positions before giving today's brief
- Claude checks available margin before recommending position size
- Claude reads last 5 sessions' actual trade history from Zerodha
  (cross-reference with TradeOS DB for accuracy check)

**Architecture:**
- kite-mcp-server runs as sidecar on VPS
- Claude pre-market brief makes MCP calls to read account state
- READ ONLY via MCP — all order execution stays on pykiteconnect direct
- MCP is for Claude's awareness, not for execution

**Why not use MCP for execution:**
pykiteconnect direct is faster, more reliable, fully under our control.
MCP adds a translation layer inappropriate for live order placement.

---

### 2.3 — Dynamic Strategy Selection

**What it does:**
Claude's pre-market brief selects which strategy to run today
based on market regime detection.

**Strategies:**
- S1: Intraday Momentum (trending days) ← V1.0, already built
- S2: Swing (multi-day, NRML) ← to be designed
- S3: Positional (weekly trend, NRML) ← to be designed
- S4: Event-Driven (results, announcements) ← to be designed

**Regime detection (Claude reads):**
- VIX level → high VIX = reduce position size, avoid momentum
- Nifty trend (weekly) → trending vs ranging regime
- Market breadth → advance/decline ratio
- Options data → PCR, max pain

**Output:**
```json
{
  "active_strategies": ["S1"],
  "regime": "trending_bullish",
  "vix_level": 14.2,
  "confidence": "high"
}
```

---

### 2.4 — TradeOS Observer (Dashboard)

**What it is:**
Personal webapp — read-only window into TradeOS performance.
Reads directly from TimescaleDB. No trading logic.

**V1.0 of Observer:**
- Trade history: every trade with entry/exit/reason/indicators
- Strategy stats: win rate, Sharpe, drawdown, profit factor (rolling)
- System health: kill switch level, WS status, session log
- Mobile + desktop (both equally important)

**V1.1 of Observer:**
- Live P&L curve (updates per fill)
- Open position MTM (mark-to-market)

**Future — Product version:**
- Multi-user support (sell to other algo traders)
- Multi-strategy view
- Clean UI for non-technical users
- Subscription model

**Stack:**
- React + Vite + Tailwind (same as arushai.com)
- FastAPI backend (reads TimescaleDB)
- WebSocket for live P&L updates
- Hosted on VPS (same server as TradeOS)

**Important:** The database schema in V1.0 is already designed
with Observer in mind. signals table has full indicator snapshots.
trades table has slippage, charges, exit_reason. Everything Observer
needs is already being written — just not displayed yet.

---

### 2.5 — Multi-Capital Scaling

**Milestone gates before scaling:**
- 4 weeks live with S1 → consistent performance → ₹1L
- 3 months live → drawdown < 10% actual → ₹3L
- 6 months live → Sharpe > 1.5 actual → ₹5L full deployment
- Observer dashboard live + working → consider external users

---

## What Claude Code Should Know

When working on any V2.0 feature:

1. **Never modify D1-D9 skills** to accommodate V2.0 features.
   The reliability layer is sacred. V2.0 adds ON TOP of it.

2. **Claude brief output is advisory only.** If Claude says
   "avoid HDFC" and an HDFC signal fires, the system skips it.
   If the Claude brief fails entirely, V1.0 defaults apply.
   Trading must never depend on LLM availability.

3. **MCP is read-only.** kite-mcp-server is never used for
   order placement. pykiteconnect direct owns all execution.

4. **Observer reads DB, never writes.** The dashboard has
   zero write access to TradeOS state or orders.

5. **Strategy modules are isolated.** S2/S3/S4 are separate
   strategy_engine/ submodules. Adding S2 cannot break S1.

---

## Version History

| Version | Status | Date |
|---------|--------|------|
| V1.0 Skills (D1-D9) | Complete | March 2026 |
| V1.0 Data Engine | Complete | March 2026 |
| V1.0 Strategy Engine | Complete | March 2026 |
| V1.0 Risk Manager | Complete | March 2026 |
| V1.0 Execution Engine | Complete | March 2026 |
| V1.0 main.py | Complete | March 2026 |
| V1.0 First Boot | Complete | March 2026 — all engines, 0 errors |
| V1.0 Paper Session 01 | Complete | 2026-03-06 — 6hr uptime, 129k ticks, 340 candles, 2 bugs fixed, 0 signals (expected) |
| V1.0 Paper Trading | In Progress | Session 02 Monday — first real signal evaluation |
| V1.0 Live ₹50K | Pending | - |
| V2.0 | Locked — not started | - |

---

## AgentBoard — Future ARUSHAI Product (Idea Locked: March 2026)

> Origin: Emerged while building TradeOS Observer with parallel
> Claude Code (backend) + Gemini CLI (frontend) agents.
> The problem: human had to manually bridge agent communications.
> The insight: this bridging problem is universal to any multi-agent build.

### What It Is

An agent-agnostic coordination workspace for multi-agent software teams.
Structured communication channel where AI agents post work, ask questions,
and read each other's output — with the human providing minimal approval
input (yes/no, one-line decisions) rather than copy-pasting context.

### The Problem It Solves

Today: Human manually bridges agents
  Agent A completes work → posts to human → human reads →
  copies to Agent B's session → Agent B responds →
  human copies back → repeat for every dependency

With AgentBoard:
  Agent A completes work → posts to AgentBoard →
  Agent B reads AgentBoard → builds → posts questions →
  Human gets Telegram: "Frontend asks: polling or push?" →
  Human replies: "polling" → Agent B reads → continues
  Human total effort: 1-line replies instead of copy-paste sessions

### Core Features (V1.0)

  Projects           — namespace per project (TradeOS, MVPL, etc.)
  Threads            — #backend, #frontend, #decisions per project
  Contracts          — shared API specs, state docs (like CONTRACTS.md but live)
  Status Board       — agent work status: TODO / IN PROGRESS / DONE / BLOCKED
  Human Gate         — certain actions require human approval (yes/no/1-line)
  Agent Identity     — CC posts as "Claude Code", Gemini as "Gemini CLI", etc.
  Telegram bridge    — human approvals via Telegram (not a web UI required)

### Core Features (V2.0 — Multi-tenancy)

  Multiple teams     — each ARUSHAI client gets their own workspace
  Audit trail        — full log of all agent decisions + human approvals
  Agent marketplace  — plug in any agent: CC, Gemini, custom OpenClaw agents
  Subscription model — SaaS for AI-native software teams

### Why ARUSHAI Builds This

  Fits mission: McKinsey-grade ops for SMBs who can't afford a full eng team
  Target user: solo founders / small teams using AI agents to build products
  Moat: agent-agnostic protocol + Telegram-native approval UX
  Timing: multi-agent workflows are becoming the norm (2026+)

### Build Gate

  DO NOT build until:
  □ TradeOS V1.0 is live and trading
  □ TradeOS Observer V1.0 is deployed
  □ At least 1 external client using ARUSHAI services
  The idea is validated. The timing is not yet right.

### Temporary Solution (for now)

  CONTRACTS.md in GitHub + human bridging.
  Sufficient for 2-agent tasks (CC + Gemini).
  Human bridging cost: ~5-10 minutes per build session.
  Not worth engineering a full system to eliminate 10 minutes.
