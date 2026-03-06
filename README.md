# TradeOS

AI-powered algorithmic trading system for NSE.
Paper trading mode — V1.0, March 2026.

## Quick Start (any machine)

### Prerequisites

- Python 3.11+
- Docker Desktop (Mac) or Docker CE (Linux)
- Zerodha account with API access (Connect app)

### Setup (one time)

```bash
git clone https://github.com/arushai-hq/tradeOS.git
cd tradeOS
bash scripts/setup.sh            # creates .venv, installs deps
bash scripts/db_start.sh         # starts TimescaleDB via Docker
bash scripts/db_migrate.sh       # applies schema (5 tables)
cp config/secrets.yaml.template config/secrets.yaml
nano config/secrets.yaml         # fill in Zerodha credentials
```

### Daily workflow

```bash
source activate.sh                              # activate venv
python scripts/refresh_token.py                 # 90s token refresh
tmux new -s tradeos
python main.py 2>&1 | tee logs/paper_session_XX.log
```

### Monitor

```bash
tmux attach -t tradeos                          # watch live logs
tail -f logs/paper_session_XX.log | grep -v tick_storage_flushed
```

## Architecture

TradeOS uses a 4-layer pipeline with 9 reliability disciplines (D1–D9):

```
Data Engine  →  Strategy Engine  →  Risk Manager  →  Execution Engine
(KiteConnect)   (S1 Momentum)       (Kill Switch)    (Paper Orders)
```

**5 concurrent asyncio tasks (D6):** `ws_listener` · `signal_processor` · `order_monitor` · `risk_watchdog` · `heartbeat`

**S1 strategy:** EMA9/21 crossover + VWAP filter + RSI 55–70 (long) / 30–45 (short) + volume ratio ≥ 1.5×

## Project Structure

```
tradeOS/
├── config/           settings.yaml, secrets.yaml.template, nse_holidays.yaml
├── data_engine/      WebSocket feed, TickValidator (5-gate), tick storage
├── strategy_engine/  CandleBuilder, IndicatorEngine, S1SignalGenerator
├── risk_manager/     PositionSizer, LossTracker, PnlTracker, kill switch
├── execution_engine/ OrderStateMachine (8 states), paper order placement
├── utils/            time_utils, telegram, db_events
├── docker/           docker-compose.yml, DB infrastructure docs
├── scripts/          setup.sh, refresh_token.py, db_*.sh
├── tests/            178 unit + integration tests (12 skipped without DB)
├── logs/             session logs (gitignored)
├── docs/             strategy specs, architecture diagrams, brainstorm notes
├── main.py           entry point — D9 session lifecycle (Phase 0–3)
├── schema.sql        TimescaleDB schema (5 tables)
└── requirements.txt
```

## Status

| Item | State |
|------|-------|
| Phase 1 Paper Trading | Active (March 2026) |
| Test suite | 178 passing, 12 skipped (DB_DSN) |
| Infrastructure | TimescaleDB + Docker Compose |
| VPS | Rocky Linux 9.7 |
| Session 01 | 6hr uptime · 129k ticks · 340 candles · 0 signals (VWAP bug fixed) |

## Key Commands

| Task | Command |
|------|---------|
| Setup environment | `bash scripts/setup.sh` |
| Start database | `bash scripts/db_start.sh` |
| Stop database | `bash scripts/db_stop.sh` |
| Apply schema | `bash scripts/db_migrate.sh` |
| Refresh token | `python scripts/refresh_token.py` |
| Check token | `python scripts/verify_token.py` |
| Run TradeOS | `python main.py` |
| Run tests | `python -m pytest tests/` |
| DB logs | `docker compose -f docker/docker-compose.yml logs -f` |

## Risk Rules (Non-Negotiable)

| Rule | Value |
|------|-------|
| Max loss per trade | 1.5% of S1 capital |
| Max daily loss | 3.0% — triggers kill switch |
| Max open positions | 3 |
| Hard intraday exit | 15:00 IST |
| Stop-loss | Mandatory on every order |

## Broker

Zerodha KiteConnect API — NSE Equities only. No F&O until Phase 3.

---

*Arushai Systems Private Limited — TradeOS*
