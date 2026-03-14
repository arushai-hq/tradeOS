# CLAUDE.md -- TradeOS Operating Instructions

> This file is auto-read by Claude Code on every session start.
> Do not modify without syncing with the Web UI brainstorm session.

---

## Project Identity

**TradeOS** -- AI-powered systematic trading system, Indian markets (NSE/BSE).
**Owner:** Irfan -- Arushai Systems Private Limited, Doha, Qatar.
**Stack:** Python 3.11 | Zerodha KiteConnect | asyncio | TimescaleDB | structlog
**Current Phase:** Phase 1 -- S1 Intraday Momentum + Paper Trade only.
**Repo:** `arushai-hq/tradeOS` | **Branch:** `main` (production)

---

## Read These Files First

| What you're building | Read this first |
|----------------------|----------------|
| Anything | `TradeOS_context.md` -- living document, current state + decisions |
| Any component | `docs/diagrams/reliability/README.md` -- 9 disciplines (D1-D9) |
| S1 Strategy | `docs/strategy_specs/S1_intraday_momentum.md` |
| Risk / Kill Switch | `docs/diagrams/reliability/README.md` -> D1 + D7 |
| Config / secrets | `config/settings.yaml` + `config/secrets.yaml.template` |
| Architecture | `docs/brainstorm/session_001_architecture.md` |
| HAWK AI | `docs/hawk_spec.md` |

---

## How This Project Is Run

**Two-role workflow:**
- **Claude.ai Web UI** -> brainstorming, architecture, decisions, diagram generation
- **Claude Code (you)** -> receives precise prompts from Web UI, writes code, runs tests

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

### 2. Secrets
- Never write API keys, tokens, or passwords in any file
- Secrets go in `config/secrets.yaml` (gitignored)
- Use `config/secrets.yaml.template` as the reference template

### 3. Reliability Disciplines (D1-D9)

**D1 -- Kill Switch:** 3-level kill switch. Every order path checks kill switch state.
**D2 -- Order State Machine:** 8 states. Never treat order as FILLED until broker confirms.
**D3 -- WebSocket Resilience:** Auto-reconnect with exponential backoff (2->30s cap).
**D5 -- Data Validation:** 5-gate TickValidator before any strategy logic.
**D6 -- Async Architecture:** All I/O non-blocking. 5 concurrent asyncio tasks.
**D7 -- Reconciliation:** Startup + every 30 min. Zerodha is always source of truth.
**D9 -- Session Guardian:** Pre-market gate -> startup -> trading -> EOD lifecycle.

### 4. Logging
```python
log.info("order_placed", order_id=order_id, strategy="s1", symbol=symbol, qty=qty)
```
Use `structlog`. Date-based files: `logs/{module}/{module}_{YYYY-MM-DD}.log`.
No bare `print()` statements in production code.

### 5. Tests Are Mandatory
Every new module gets a corresponding test file. Minimum coverage:
- Happy path works
- Kill switch blocks when active
- Bad input is rejected gracefully
- All tests must pass before commit

### 6. Risk Rules Are Constants
```python
MAX_LOSS_PER_TRADE_PCT = 0.015   # 1.5%
MAX_DAILY_LOSS_PCT     = 0.030   # 3.0%
MAX_OPEN_POSITIONS     = 4
HARD_EXIT_TIME         = "15:00"  # IST
```

---

## File Structure

```
bin/tradeos               Unified CLI entry point (bash shim)
main.py                   D9 session lifecycle
config/                   settings.yaml (committed) + secrets.yaml (gitignored)
data_engine/              WebSocket feed, tick validator, tick storage
strategy_engine/          CandleBuilder, indicators, S1 signal generator, risk gates
risk_manager/             Kill switch, position sizer, PnL tracker
execution_engine/         Order state machine, paper order placement
regime_detector/          4-regime classifier
hawk_engine/              HAWK AI market intelligence
utils/                    telegram_notifier, time_utils, db_events
tools/                    session_report, hawk CLI, hawk_eval, db_backfill
scripts/                  token_cron, token_server, log_rotation, setup scripts
docker/                   docker-compose.yml, nginx config, SSL
migrations/               SQL migration files
tests/                    All pytest tests (mirrors module structure)
logs/                     Log output (gitignored)
docs/                     Strategy specs, architecture, brainstorm notes
```

---

## Key Patterns

| Pattern | Implementation |
|---------|---------------|
| Logging | `structlog` with JSON output |
| Database | `asyncpg` (async PostgreSQL) with TimescaleDB |
| Broker API | `pykiteconnect` v5 (Zerodha KiteConnect) |
| Config | `config/settings.yaml` (yaml.safe_load) |
| Credentials | `config/secrets.yaml` (gitignored) |
| Time zones | `pytz.timezone("Asia/Kolkata")` -- never `datetime.now()` |
| CLI | `bin/tradeos` bash shim -- all ops go through this in production |
| Telegram | `utils/telegram_notifier.py` -- config-driven, hot-reload |

---

## Git Conventions

### Branching
- `main` = production (deployed on VPS, always deployable)
- `feature/*` = new features (created from main)
- `fix/*` = bug fixes
- Merge to main only when all tests pass

### Commit Messages
```
feat: add TickValidator with 5-gate filter
test: add unit tests for RiskManager daily loss trigger
fix: order state machine not handling PARTIALLY_FILLED
docs: update TradeOS_context.md with session results
refactor: extract VWAP calculation to indicators module
```

### Testing
```bash
tradeos test -x -q                    # Quick test run
python -m pytest tests/ -x -q         # Direct pytest
```
Current: 489 passing, 12 skipped.

---

## Session Rules

1. **Nemawashi First** -- 70-80% planning, 20-30% implementation. No rushing to code.
2. **Living Document Protocol** -- Every decision captured in `TradeOS_context.md` via delta.
3. **Context Handoff** -- If session approaches limits, update `TradeOS_context.md` with resume point.
4. **Allocation Sum Rule** -- Strategy allocations in settings.yaml must sum to 1.00.
5. **Position Sizing Parameters** -- All configured in settings.yaml, startup-validated.
6. **Context Hygiene** -- `TradeOS_context.md` is a rolling window. Archive old items.
7. **Telegram Channel Separation** -- Each module gets its own channel. Never mix streams.
8. **Git Branching** -- feature/* from main, merge only when fully tested.
9. **SHORT Position Accounting** -- Negative qty for shorts. Verify field names always.
10. **Log File Convention** -- Date-based: `logs/{module}/{module}_{YYYY-MM-DD}.log`.
11. **CLI Convention** -- All operations via `tradeos <command>`. Never call Python scripts directly.
12. **Documentation Convention** -- README.md and CLAUDE.md updated with every feature addition.

---

## What NOT to Do

- Do not write blocking I/O in the asyncio event loop
- Do not place live orders in paper mode
- Do not commit `config/secrets.yaml` or files in `logs/`
- Do not skip writing tests for a module
- Do not change `mode: live` in settings.yaml
- Do not build Phase 2+ components until Phase 1 is tested
- Do not use bare `except:` -- always catch specific exceptions
- Do not use `time.sleep()` in async code -- use `await asyncio.sleep()`
- Do not call Python scripts directly in production -- use `tradeos` CLI
- Do not invent risk parameters -- use constants from `config/settings.yaml`

---

*Arushai Systems Private Limited -- TradeOS*
*Web UI brainstorm session -> Claude Code execution pipeline*

# context-mode — MANDATORY routing rules

You have context-mode MCP tools available. These rules are NOT optional — they protect your context window from flooding. A single unrouted command can dump 56 KB into context and waste the entire session.

## BLOCKED commands — do NOT attempt these

### curl / wget — BLOCKED
Any Bash command containing `curl` or `wget` is intercepted and replaced with an error message. Do NOT retry.
Instead use:
- `ctx_fetch_and_index(url, source)` to fetch and index web pages
- `ctx_execute(language: "javascript", code: "const r = await fetch(...)")` to run HTTP calls in sandbox

### Inline HTTP — BLOCKED
Any Bash command containing `fetch('http`, `requests.get(`, `requests.post(`, `http.get(`, or `http.request(` is intercepted and replaced with an error message. Do NOT retry with Bash.
Instead use:
- `ctx_execute(language, code)` to run HTTP calls in sandbox — only stdout enters context

### WebFetch — BLOCKED
WebFetch calls are denied entirely. The URL is extracted and you are told to use `ctx_fetch_and_index` instead.
Instead use:
- `ctx_fetch_and_index(url, source)` then `ctx_search(queries)` to query the indexed content

## REDIRECTED tools — use sandbox equivalents

### Bash (>20 lines output)
Bash is ONLY for: `git`, `mkdir`, `rm`, `mv`, `cd`, `ls`, `npm install`, `pip install`, and other short-output commands.
For everything else, use:
- `ctx_batch_execute(commands, queries)` — run multiple commands + search in ONE call
- `ctx_execute(language: "shell", code: "...")` — run in sandbox, only stdout enters context

### Read (for analysis)
If you are reading a file to **Edit** it → Read is correct (Edit needs content in context).
If you are reading to **analyze, explore, or summarize** → use `ctx_execute_file(path, language, code)` instead. Only your printed summary enters context. The raw file content stays in the sandbox.

### Grep (large results)
Grep results can flood context. Use `ctx_execute(language: "shell", code: "grep ...")` to run searches in sandbox. Only your printed summary enters context.

## Tool selection hierarchy

1. **GATHER**: `ctx_batch_execute(commands, queries)` — Primary tool. Runs all commands, auto-indexes output, returns search results. ONE call replaces 30+ individual calls.
2. **FOLLOW-UP**: `ctx_search(queries: ["q1", "q2", ...])` — Query indexed content. Pass ALL questions as array in ONE call.
3. **PROCESSING**: `ctx_execute(language, code)` | `ctx_execute_file(path, language, code)` — Sandbox execution. Only stdout enters context.
4. **WEB**: `ctx_fetch_and_index(url, source)` then `ctx_search(queries)` — Fetch, chunk, index, query. Raw HTML never enters context.
5. **INDEX**: `ctx_index(content, source)` — Store content in FTS5 knowledge base for later search.

## Subagent routing

When spawning subagents (Agent/Task tool), the routing block is automatically injected into their prompt. Bash-type subagents are upgraded to general-purpose so they have access to MCP tools. You do NOT need to manually instruct subagents about context-mode.

## Output constraints

- Keep responses under 500 words.
- Write artifacts (code, configs, PRDs) to FILES — never return them as inline text. Return only: file path + 1-line description.
- When indexing content, use descriptive source labels so others can `ctx_search(source: "label")` later.

## ctx commands

| Command | Action |
|---------|--------|
| `ctx stats` | Call the `ctx_stats` MCP tool and display the full output verbatim |
| `ctx doctor` | Call the `ctx_doctor` MCP tool, run the returned shell command, display as checklist |
| `ctx upgrade` | Call the `ctx_upgrade` MCP tool, run the returned shell command, display as checklist |
