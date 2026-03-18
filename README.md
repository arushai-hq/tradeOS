# TradeOS

AI-powered algorithmic trading system for NSE intraday equities.

**Stack:** Python 3.11 | Zerodha KiteConnect | asyncio | TimescaleDB | Docker
**Phase:** Paper trading (March 2026) | **Mode:** `paper` only
**Compliance:** OSD v1.9.0 (29 standards) | ASPS v1.3.0 (Pattern B / HEAVY tier)

---

## Quick Start

### Prerequisites

- Python 3.11+
- Docker Desktop (Mac) or Docker CE (Linux)
- tmux
- Zerodha account with API access (KiteConnect app)

### Setup (one-time)

```bash
git clone https://github.com/arushai-hq/tradeOS.git
cd tradeOS

# Environment
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Database
docker compose -f docker/docker-compose.yml up -d

# Config
cp config/secrets.yaml.template config/secrets.yaml
# Edit config/secrets.yaml — fill in Zerodha API key/secret, Telegram tokens

# Install CLI
bash scripts/install_tradeos_cli.sh
```

### Daily workflow

```bash
tradeos preflight              # Pre-market systems check
tradeos auth                   # Token authentication (or auto via cron)
tradeos start                  # Start trading in tmux session
tradeos status                 # Check system health
tradeos stop                   # Graceful shutdown at EOD
tradeos report auto            # End-of-day report + verify
```

---

## CLI Reference

All operations go through the `tradeos` command. Set `TRADEOS_DIR` to override the base directory (default: `/opt/tradeOS`).

### Trading

| Command | Description |
|---------|-------------|
| `tradeos auth` | Run daily token authentication (token_cron.py) |
| `tradeos auth-server` | Start token callback server for manual auth |
| `tradeos start` | Start main.py in a named tmux session |
| `tradeos stop` | Graceful stop (Ctrl+C, force kill after 3s) |
| `tradeos restart` | Stop then start |
| `tradeos status` | Show trading process, token, DB, nginx status |
| `tradeos preflight` | Pre-market health check (8 checks, go/no-go) |

### Reports

| Command | Description |
|---------|-------------|
| `tradeos report <logfile>` | Session report from log file |
| `tradeos report --source db --date DATE` | Session report from database |
| `tradeos report --verify <logfile>` | Cross-check log vs DB |
| `tradeos report <logfile> --export csv` | Export to CSV or xlsx |
| `tradeos report auto` | EOD auto-report: DB report + log vs DB verify |

### HAWK (AI Market Intelligence)

| Command | Description |
|---------|-------------|
| `tradeos hawk run --run evening` | Run HAWK evening analysis |
| `tradeos hawk run --run morning` | Run HAWK morning update |
| `tradeos hawk eval` | Evaluate yesterday's picks |
| `tradeos hawk eval --date YYYY-MM-DD` | Evaluate specific date |

### Data (Backtest)

| Command | Description |
|---------|-------------|
| `tradeos data download --interval 15min --days 1095` | Download 15min candles (3 years) |
| `tradeos data download --all` | Download all 5 intervals with recommended durations |
| `tradeos data download --symbol RELIANCE --interval 15min --days 200` | Single symbol |
| `tradeos data status` | Show download coverage per interval |

### Futures Data

| Command | Description |
|---------|-------------|
| `tradeos futures download --all` | Download all intervals for NIFTY + BANKNIFTY (18 months) |
| `tradeos futures download --interval 15min --days 548` | Download 15min futures candles |
| `tradeos futures download --instrument NIFTY` | Single instrument |
| `tradeos futures status` | Show futures download coverage |

### Futures Backtesting

| Command | Description |
|---------|-------------|
| `tradeos futures backtest run --instrument NIFTY --strategy s1v2` | Run S1v2 futures backtest on NIFTY |
| `tradeos futures backtest run --instrument BANKNIFTY --strategy s1v3 --interval 15min` | S1v3 on BANKNIFTY |
| `tradeos futures backtest run --instrument NIFTY --strategy s1v2 --exit-mode trailing` | Trailing stop mode |
| `tradeos futures backtest compare --instrument NIFTY --strategy s1v2 --modes fixed,trailing,partial` | Compare exit modes |
| `tradeos futures backtest optimize --instrument NIFTY --strategy s1v2 --param atr_mult --range 1.0:0.5:3.0` | Parameter sweep |
| `tradeos futures backtest run --instrument NIFTY --strategy orb --interval 5minute` | ORB strategy on 5min candles |
| `tradeos futures backtest run --instrument NIFTY --strategy vwap_mr --interval 5minute` | VWAP Mean Reversion |
| `tradeos futures backtest run --instrument NIFTY --strategy macd_st --interval 15minute` | MACD + Supertrend (multi-TF) |
| `tradeos futures backtest show --last-run` | Show most recent futures run |
| `tradeos futures backtest show --run-id 5` | Show specific run by ID |

**Futures strategies:** `s1v2`, `s1v3` (equity-ported), `orb` (Opening Range Breakout), `vwap_mr` (VWAP Mean Reversion), `macd_st` (MACD + Supertrend)

### Backtesting

| Command | Description |
|---------|-------------|
| `tradeos backtest run --from 2025-09-01 --to 2026-03-16` | Run S1 backtest (fixed exit mode) |
| `tradeos backtest run --exit-mode trailing --atr-mult 1.5` | Trailing stop with ATR |
| `tradeos backtest run --exit-mode partial --partial-pct 0.5` | Partial exit at 1R + trail |
| `tradeos backtest optimize --param atr_multiplier --range 1.0:0.25:3.0` | Parameter sweep |
| `tradeos backtest compare --modes fixed,trailing,partial` | Compare exit modes |
| `tradeos backtest show --last-run` | Show most recent backtest results |
| `tradeos backtest show --run-id 5` | Show specific run by ID |

### System

| Command | Description |
|---------|-------------|
| `tradeos logs tail` | Tail today's trading log |
| `tradeos logs list` | List recent log files (tradeos/hawk/token) |
| `tradeos logs rotate` | Run log rotation (compress/delete old logs) |
| `tradeos db shell` | Open psql shell in TimescaleDB container |
| `tradeos db migrate` | Run SQL migrations from `migrations/` |
| `tradeos docker up\|down\|ps\|logs` | Docker compose pass-through |
| `tradeos config show` | Print settings.yaml |
| `tradeos config validate` | Check mode, allocation sum, required sections |
| `tradeos config secrets` | Print secrets.yaml with values redacted |
| `tradeos cron install` | Install token auth + log rotation cron entries |
| `tradeos cron status` | Show cron entry install status |
| `tradeos test [args]` | Run pytest with pass-through args |
| `tradeos version` | Show CLI version, Python, platform |

---

## Architecture

4-layer pipeline with 9 reliability disciplines (D1-D9):

```
Data Engine  ->  Strategy Engine  ->  Risk Manager  ->  Execution Engine
(KiteConnect)    (S1 Momentum)       (Kill Switch)     (Paper Orders)
```

**5 concurrent asyncio tasks (D6):** `ws_listener` | `signal_processor` | `order_monitor` | `risk_watchdog` | `heartbeat`

### Module Overview

Engine modules live under `core/` (ASPS Pattern B structure):

| Module | Role |
|--------|------|
| `core/data_engine/` | WebSocket feed, 5-gate tick validator, tick storage |
| `core/strategy_engine/` | CandleBuilder, indicators, S1 signal generator, risk gates |
| `core/risk_manager/` | Kill switch (3 levels), position sizer, P&L tracker |
| `core/execution_engine/` | Order state machine (8 states), paper order placer |
| `core/regime_detector/` | 4-regime classifier (BULL/BEAR/HIGH_VOL/CRASH) |
| `tools/hawk_engine/` | HAWK AI engine, multi-model consensus (4 LLMs) |
| `main.py` | D9 session lifecycle: pre-market gate -> startup -> trading -> EOD |
| `bin/tradeos` | Unified CLI entry point (bash shim) |
| `tools/` | session_report, hawk, hawk_eval, db_backfill |
| `utils/progress.py` | CLI spinner/progress indicators (NO_COLOR, isatty aware) |
| `scripts/` | token_cron, token_server, log_rotation, setup_cron, setup_ssl |
| `docker/` | docker-compose (TimescaleDB + nginx + certbot) |
| `docs/decisions/` | Architecture Decision Records ([ADRs](docs/decisions/)) |
| `docs/runbooks/` | Operational procedures ([runbooks](docs/runbooks/)) |

---

## Configuration

### config/settings.yaml (committed)

Key sections: `system` (mode), `capital` (total + allocation), `risk` (loss limits), `strategy.s1` (EMA/RSI/VWAP params), `trading_hours`, `watchlist` (50 NIFTY 50 stocks), `trading.instruments` (token map), `token_automation`, `log_rotation`. HAWK data sources: KiteConnect (primary) → nsetools/nsepython (fallback). Run `python scripts/fetch_instrument_tokens.py --verify` to check token freshness.

### config/secrets.yaml (gitignored)

Copy from `config/secrets.yaml.template`. Required keys:
- `zerodha.api_key`, `zerodha.api_secret` — KiteConnect credentials
- `telegram.trading.bot_token`, `telegram.trading.chat_id` — Trade alerts
- `telegram.hawk.bot_token`, `telegram.hawk.chat_id` — HAWK picks channel
- `llm.anthropic.api_key` or `llm.openrouter.api_key` — For HAWK AI engine

---

## Production Setup

### SSL + Nginx (one-time)

```bash
bash scripts/setup_ssl.sh your-email@example.com
```

Sets up Nginx reverse proxy on port 11443 with Let's Encrypt SSL. Proxies `/callback` to token_server for Zerodha OAuth.

**Networking note:** Nginx (in Docker on `tradeos_network` bridge 172.20.0.0/16) proxies to `token_server.py` on the host via the VPS public IP. `host.docker.internal` is not used — it resolves to the `docker0` bridge (172.17.0.1) which is unreachable from custom Docker networks.

### Cron (one-time)

```bash
tradeos cron install           # Install token auth + log rotation crons
tradeos cron status            # Verify installation
```

- **Token auth:** Mon-Fri 07:00 IST (starts token_server, sends login URL to Telegram)
- **Log rotation:** Sunday 02:00 IST (compress >30 days, delete >90 days)

### Log Files

All modules write date-based logs: `logs/{module}/{module}_{YYYY-MM-DD}.log`

Subdirectories: `tradeos/` (trading), `hawk/` (AI engine), `token/` (auth)

---

## Development Tools

### context-mode (Claude Code plugin)

[context-mode](https://github.com/mksglu/context-mode) is installed as a Claude Code MCP plugin to optimize context window usage during extended build sessions. It sandboxes raw command output and web fetches into a SQLite + FTS5 index, reducing context consumption by ~98%.

- **Resume sessions:** Use `--continue` flag when resuming Claude Code to carry forward indexed context.
- **Hooks:** `curl`/`wget` and large Bash output are automatically routed through sandbox tools (`ctx_execute`, `ctx_fetch_and_index`).
- **Existing MCP servers** (KiteConnect, TimescaleDB) are unaffected.

### Claude Code Skills

13 TradeOS-specific skills in `.claude/skills/` enforce reliability disciplines and project conventions:

- **tradeos-architecture** — System architecture, module map, data flow
- **tradeos-gotchas** — Bug catalogue (B1-B14), field name traps, P&L pitfalls
- **tradeos-testing** — Test standards, conventions, regression test rules
- **tradeos-operations** — VPS deployment, daily workflow, CLI reference
- **D1-D9 discipline skills** — Kill switch, order state machine, WebSocket resilience, observability, tick validator, async architecture, position reconciliation, test pyramid, session guardian

---

## Testing

```bash
tradeos test                   # Run all tests
tradeos test -x -q             # Stop on first failure, quiet
tradeos test tests/unit/ -v    # Run unit tests, verbose
```

Current: **524 passing**, 12 skipped, 0 failures.

---

## Project Status

| Item | Status |
|------|--------|
| S1 Intraday Momentum | DEPRECATED — negative expectancy confirmed via backtester |
| S1v2 Trend Pullback | In development — backtester implementation pending |
| S1v3 Mean Reversion | In development — backtester implementation pending |
| Paper sessions | 9 completed (13 trades, -₹1,762 cumulative net P&L) |
| HAWK AI Engine | Active (evening + morning runs) |
| Backtester | Operational — 2.75M candles, optimizer, compare modes |
| CLI | v0.2.0 (25+ subcommands) |
| Infrastructure | TimescaleDB + Docker + Nginx + SSL + cron |
| Tests | 579 passing |
| VPS | Rocky Linux 9.7 |
| Mode | `paper` (never auto-switched to live) |

### Strategy Status

- **S1 (EMA Crossover Momentum):** Deprecated — negative expectancy confirmed via backtester. Running for infrastructure validation only.
- **S1v2 (Trend Pullback):** In development — backtester implementation pending. See `docs/strategy_specs/strategy_spec_s1v2_s1v3.md`.
- **S1v3 (Mean Reversion):** In development — backtester implementation pending. See `docs/strategy_specs/strategy_spec_s1v2_s1v3.md`.

---

## Risk Rules (Non-Negotiable)

| Rule | Value |
|------|-------|
| Max loss per trade | 1.5% of S1 capital |
| Max daily loss | 3.0% (triggers kill switch) |
| Max open positions | 6 |
| Hard intraday exit | 15:00 IST |
| Stop-loss | Mandatory on every order |
| Mode | Paper only until all gates pass |

---

## Project Structure

This project follows **ASPS v1.0.0** (ARUSHAI Standard Project Structure) — Pattern B (Engine + Tools), HEAVY tier. Engine modules are organized under `core/`, with subdirectory `CLAUDE.md` files for skill routing. See [docs/decisions/](docs/decisions/) for architecture decision records and [docs/runbooks/](docs/runbooks/) for operational procedures.

---

## License

Proprietary. Arushai Systems Private Limited.
