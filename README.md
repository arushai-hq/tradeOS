# TradeOS

AI-powered algorithmic trading system for NSE intraday equities.

**Stack:** Python 3.11 | Zerodha KiteConnect | asyncio | TimescaleDB | Docker
**Phase:** Paper trading (March 2026) | **Mode:** `paper` only

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

| Module | Role |
|--------|------|
| `data_engine/` | WebSocket feed, 5-gate tick validator, tick storage |
| `strategy_engine/` | CandleBuilder, indicators, S1 signal generator, risk gates |
| `risk_manager/` | Kill switch (3 levels), position sizer, P&L tracker |
| `execution_engine/` | Order state machine (8 states), paper order placer |
| `regime_detector/` | 4-regime classifier (BULL/BEAR/HIGH_VOL/CRASH) |
| `hawk_engine/` | HAWK AI engine, multi-model consensus (4 LLMs) |
| `main.py` | D9 session lifecycle: pre-market gate -> startup -> trading -> EOD |
| `bin/tradeos` | Unified CLI entry point (bash shim) |
| `tools/` | session_report, hawk, hawk_eval, db_backfill |
| `scripts/` | token_cron, token_server, log_rotation, setup_cron, setup_ssl |
| `docker/` | docker-compose (TimescaleDB + nginx + certbot) |

---

## Configuration

### config/settings.yaml (committed)

Key sections: `system` (mode), `capital` (total + allocation), `risk` (loss limits), `strategy.s1` (EMA/RSI/VWAP params), `trading_hours`, `watchlist` (20 NSE stocks), `token_automation`, `log_rotation`.

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

## Testing

```bash
tradeos test                   # Run all tests
tradeos test -x -q             # Stop on first failure, quiet
tradeos test tests/unit/ -v    # Run unit tests, verbose
```

Current: **489 passing**, 12 skipped, 0 failures.

---

## Project Status

| Item | Status |
|------|--------|
| S1 Intraday Momentum | Active (paper trading) |
| Paper sessions | 7 completed (2 trades, +1,390 net P&L) |
| HAWK AI Engine | Active (evening + morning runs) |
| CLI | v0.2.0 (25+ subcommands) |
| Infrastructure | TimescaleDB + Docker + Nginx + SSL + cron |
| Tests | 489 passing |
| VPS | Rocky Linux 9.7 |
| Mode | `paper` (never auto-switched to live) |

---

## Risk Rules (Non-Negotiable)

| Rule | Value |
|------|-------|
| Max loss per trade | 1.5% of S1 capital |
| Max daily loss | 3.0% (triggers kill switch) |
| Max open positions | 4 |
| Hard intraday exit | 15:00 IST |
| Stop-loss | Mandatory on every order |
| Mode | Paper only until all gates pass |

---

## License

Proprietary. Arushai Systems Private Limited.
