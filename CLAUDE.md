# TradeOS

AI-powered systematic trading system for NSE intraday equities.
Python 3.11 | Zerodha KiteConnect | asyncio | TimescaleDB | structlog
Repo: `arushai-hq/tradeOS` | Phase 1: S1 Intraday Momentum | Mode: `paper`

## Commands

```bash
tradeos test -x -q               # Run tests (499 passing)
tradeos start                    # Start trading in tmux
tradeos stop                     # Graceful shutdown
tradeos preflight                # Pre-market health check
tradeos report auto              # EOD report
python -m pytest tests/ -v       # Direct pytest
```

## Structure

```
main.py              D9 session lifecycle entry point
core/                Trading engine (data_engine, strategy_engine, execution_engine, risk_manager, regime_detector)
tools/               CLI tools: session_report, HAWK AI engine, db_backfill
scripts/             Automation: token_cron, token_server, log_rotation
utils/               Shared: telegram_notifier, time_utils, position_helpers
config/              settings.yaml (committed) + secrets.yaml (gitignored)
bin/tradeos          Unified CLI entry point (bash shim)
tests/               Unit + integration tests (mirrors module structure)
docs/                Strategy specs, ADRs, runbooks, brainstorm notes
docker/              docker-compose (TimescaleDB + Nginx + SSL)
```

## Rules

1. **Nemawashi First** — 70-80% planning, 20-30% implementation
2. **Living Document** — Every decision in `TradeOS_context.md` via delta
3. **Context Handoff** — Update `TradeOS_context.md` with resume point at session limits
4. **Allocation Sum** — Strategy allocations in settings.yaml must sum to 1.00
5. **Position Sizing** — All params in settings.yaml, startup-validated
6. **Context Hygiene** — `TradeOS_context.md` is a rolling window, archive old items
7. **Telegram Separation** — Each module gets its own channel
8. **Git Branching** — feature/* from main, merge only when fully tested
9. **SHORT Accounting** — Negative qty for shorts, verify field names always
10. **Log Convention** — Date-based: `logs/{module}/{module}_{YYYY-MM-DD}.log`
11. **CLI Convention** — All ops via `tradeos <command>`, never call scripts directly
12. **Docs Convention** — README.md and CLAUDE.md updated with every feature

## Skills

| Skill | When to use |
|-------|-------------|
| tradeos-architecture | System architecture, module map, data flow |
| tradeos-gotchas | Bug patterns (B1-B14), field name traps, P&L pitfalls |
| tradeos-testing | Test standards, conventions, regression rules |
| tradeos-operations | VPS deployment, daily workflow, CLI reference |
| tradeos-kill-switch-guardian | D1: 3-level kill switch implementation |
| tradeos-order-state-machine | D2: 8-state order lifecycle |
| tradeos-websocket-resilience | D3: Auto-reconnect with exponential backoff |
| tradeos-observability | D4: structlog + Telegram + Prometheus |
| tradeos-tick-validator | D5: 5-gate tick validation pipeline |
| tradeos-async-architecture | D6: 5-task asyncio event loop |
| tradeos-position-reconciler | D7: Zerodha position reconciliation |
| tradeos-test-pyramid | D8: Three-layer testing gate |
| tradeos-session-guardian | D9: Session lifecycle management |

## Do Not

- Deploy during market hours (09:15–15:30 IST) — window: before 09:00 or after 16:00
- Commit directly to main — use feature/fix branches, merge after tests pass
- Call Python scripts directly — use `tradeos` CLI
- Change `mode: live` in settings.yaml without explicit instruction
- Write blocking I/O in asyncio event loop — use `await asyncio.sleep()` not `time.sleep()`
- Commit `config/secrets.yaml` or files in `logs/`
- Modify position accounting field names without checking SHORT handling (B7)
- Use bare `except:` — always catch specific exceptions
- Skip writing tests for any new module
- Invent risk parameters — use constants from `config/settings.yaml`

## Deep Context

| Document | Purpose |
|----------|---------|
| `TradeOS_context.md` | Living document — current state, decisions, session log |
| `core/CLAUDE.md` | Engine skill router — D1-D9 disciplines |
| `tools/CLAUDE.md` | CLI tools and HAWK AI skill router |
| `tests/CLAUDE.md` | Test conventions and commands |
| `docs/decisions/` | Architecture Decision Records |
| `docs/runbooks/` | Operational procedures |
| `docs/strategy_specs/` | Strategy specifications |
| `config/settings.yaml` | All runtime configuration |

## Two-Role Workflow

- **Claude.ai Web UI** — brainstorming, architecture, decisions
- **Claude Code** — receives prompts from Web UI, writes code, runs tests

Prompts follow: `## CONTEXT / ## TASK / ## RULES / ## ACCEPTANCE CRITERIA / ## OUTPUT`
Execute exactly as specified. Do not invent scope.

---

# context-mode — MANDATORY routing rules

Context-mode MCP tools protect the context window from flooding.

## Blocked commands

- **curl/wget** — Use `ctx_fetch_and_index(url, source)` or `ctx_execute` with fetch
- **Inline HTTP** — Use `ctx_execute(language, code)` in sandbox
- **WebFetch** — Use `ctx_fetch_and_index(url, source)` then `ctx_search(queries)`

## Tool routing

| Scenario | Use this |
|----------|----------|
| Bash >20 lines output | `ctx_batch_execute(commands, queries)` or `ctx_execute("shell", code)` |
| Read for analysis | `ctx_execute_file(path, language, code)` |
| Grep large results | `ctx_execute("shell", code)` |
| Multiple commands | `ctx_batch_execute` — ONE call replaces 30+ individual calls |
| Follow-up queries | `ctx_search(queries: ["q1", "q2"])` |
| Web content | `ctx_fetch_and_index(url)` then `ctx_search` |

Bash is ONLY for: git, mkdir, rm, mv, cd, ls, pip install, and short-output commands.
Read is correct when you need file content for Edit — otherwise use `ctx_execute_file`.

## ctx commands

| Command | Action |
|---------|--------|
| `ctx stats` | Call `ctx_stats` MCP tool, display output |
| `ctx doctor` | Call `ctx_doctor` MCP tool, run returned command |
| `ctx upgrade` | Call `ctx_upgrade` MCP tool, run returned command |
