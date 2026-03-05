# TradeOS — Reliability Engineering Diagrams

> **These 8 disciplines must be implemented before a single order is placed in production.**
> Built before any strategy code. Infrastructure first, artillery second.

---

## How to Open

Go to [excalidraw.com](https://excalidraw.com) → drag any `.excalidraw` file → fully editable.

---

## Diagram Index

| File | Discipline | Core Principle |
|------|-----------|----------------|
| `D0_master_overview.excalidraw` | Master Overview | All 8 disciplines in one view |
| `D1_kill_switch_hierarchy.excalidraw` | Kill Switch | 3 levels: Trade Stop → Position Stop → System Stop |
| `D2_order_state_machine.excalidraw` | Order State Machine | 8 states — never assume PLACED = FILLED |
| `D3_websocket_resilience.excalidraw` | WebSocket Resilience | Exponential backoff + stale signal detection |
| `D4_observability_stack.excalidraw` | Observability | Prometheus + Loki + Grafana + Telegram |
| `D5_data_validation.excalidraw` | Data Validation | 5-gate filter on every tick before strategy |
| `D6_async_architecture.excalidraw` | Async Architecture | 5 concurrent asyncio tasks, nothing blocks |
| `D7_position_reconciliation.excalidraw` | Reconciliation | Zerodha is truth — mismatch = lock instrument |
| `D8_testing_pyramid.excalidraw` | Testing Pyramid | Unit → Integration → Simulation before live |

---

## The 8 Disciplines

### D1 — Kill Switch Hierarchy
Three hardcoded emergency stops that escalate automatically:
- **Level 1** (Trade Stop): Stop new signals. Positions stay open.
- **Level 2** (Position Stop): Cancel all orders. Close all positions.
- **Level 3** (System Stop): Kill everything. Nuclear option.

Triggers: 3 consecutive losses, daily loss > 3%, WS disconnect > 60s, API errors > 5 in 5 min.
Tool: `pybreaker`

---

### D2 — Order State Machine
An order is NOT binary. It has **8 states**:
```
CREATED → SUBMITTED → ACKNOWLEDGED → PARTIALLY_FILLED → FILLED
                                   → REJECTED
                                   → PENDING_CANCEL → CANCELLED
                                   → EXPIRED
                                   → PENDING_UPDATE
```
**Critical rule:** On every system restart, query Zerodha for open orders BEFORE placing anything. Unrecognised order = LOCK instrument.
Tool: `python-statemachine`

---

### D3 — WebSocket Resilience
KiteConnect WebSocket **will** disconnect. The system must recover without human intervention.

- **Exponential backoff:** 2s → 4s → 8s → 16s → 30s cap → Telegram alert
- **Stale signal detection:** Signal fired during disconnect? Age > 5 min = Dead Signal. Discard.
- **Heartbeat monitor:** No tick in 30s = trigger reconnect

---

### D4 — Structured Logging + Observability
Three pillars:
1. **Prometheus** — Metrics: `trades_total`, `pnl_rupees`, `drawdown_pct`, `api_latency`
2. **Loki** — Logs: structured JSON, every event with `order_id`, `strategy`, `level`
3. **Grafana** — Dashboards + Alertmanager + Telegram for critical events

Phase 1: `structlog` (JSON to file) + Telegram alerts.
Phase 2: Full Prometheus + Grafana stack on VPS.

---

### D5 — Data Validation & Bad Tick Detection
Every tick passes 5 gates before touching strategy logic:
1. `price > 0` (zero filter)
2. `price within ±20% of previous close` (circuit filter)
3. `volume >= 0` (no negatives)
4. `timestamp within last 5 seconds` (staleness filter)
5. `not duplicate of last tick` (dedup filter)

**Rule:** Never halt on a bad tick. Discard silently, log it, continue. A halt is itself a vulnerability.

---

### D6 — Async-First Architecture
Five concurrent tasks running in a single `asyncio` event loop:
1. **WebSocket Listener** — ticks → queue
2. **Signal Processor** — queue → strategy engine
3. **Order Monitor** — poll Zerodha every 5s
4. **Risk Watchdog** — drawdown check every 1s
5. **Heartbeat** — proves system is alive every 30s

**Golden rule:** Any blocking I/O (REST API, file write, DB query) must use `asyncio.to_thread()`. Never block the event loop.

---

### D7 — Position Reconciliation
Aligns broker reality (Zerodha) with local state. Runs:
- At system startup (always)
- Every 30 minutes during market hours
- After any network disruption

**Mismatch handler:** LOCK instrument → LOG critical → Telegram alert → await manual resolution.
**Rule:** Zerodha is the source of truth. Never trade on an unresolved mismatch.

---

### D8 — Testing Pyramid
Three layers, all must pass before live deployment:

**Layer 1 — Unit Tests (pytest):**
- TickValidator rejects zero-price ticks
- RiskManager blocks orders at daily loss limit
- Kill switch prevents orders when active
- State machine handles all 8 order states

**Layer 2 — Integration Tests (paper trading mode):**
- P&L tracking accuracy after fills
- Reconciliation mismatch detection
- WebSocket reconnect recovery

**Layer 3 — Simulation Tests (backtesting.py):**
- S1 strategy backtested on 1yr+ NSE data
- Max drawdown simulation
- Monte Carlo: 1000 trade sequences

**Live deployment gate:** All 3 layers pass → ₹50K live deployment.

---

## Phase Deployment

| Phase | Disciplines Active | Capital |
|-------|--------------------|---------|
| Phase 1 | D1 + D2 + D3 + D5 + D6 + D7 + D8 | ₹50K live |
| Phase 2 | + D4 full observability (Prometheus + Grafana) | ₹3L+ |

---

## The Hard Truth

> **Knight Capital Group had a profitable trading strategy. They lost $440M in 45 minutes due to a deployment error — not a bad strategy.**
>
> Every firm that blew up on algo trading got killed by infrastructure failures, not bad strategies. Build the fortress first. Then install the artillery.

---

*Generated: TradeOS Brainstorm Session — Pre-Code Reliability Design*
*Repo: arushai-hq/tradeOS*
