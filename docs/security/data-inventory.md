# TradeOS — Data Inventory

> OSD v1.9.0 Standard #28. Last updated: 2026-03-16.

## Scope

TradeOS handles **market and trading data only**. It does NOT process, store, or transmit personal user data, customer data, or PII. The sole operator is the founder (single-user system).

## Data Categories

| Category | Storage | Retention | Sensitivity | Encryption |
|----------|---------|-----------|-------------|------------|
| Tick data | TimescaleDB `ticks` table | Indefinite | Low (public market data) | At-rest: PostgreSQL default |
| 15-min candles | TimescaleDB `candles_15m` table | Indefinite | Low (derived from public data) | At-rest: PostgreSQL default |
| Signals | TimescaleDB `signals` table | Indefinite | Medium (proprietary strategy output) | At-rest: PostgreSQL default |
| Trades | TimescaleDB `trades` table | Indefinite | Medium (execution records with P&L) | At-rest: PostgreSQL default |
| Sessions | TimescaleDB `sessions` table | Indefinite | Low (daily summary stats) | At-rest: PostgreSQL default |
| Application logs | File system `logs/` | 90 days (auto-rotated) | Medium (contains trade details) | None (local filesystem) |
| Config (settings) | `config/settings.yaml` (git-tracked) | Git history | Low (no secrets) | None |
| Config (secrets) | `config/secrets.yaml` (gitignored) | Single file | **High** (API keys, tokens) | None (file permissions only) |
| HAWK AI picks | JSON files + TimescaleDB | Indefinite | Low (AI-generated watchlist) | At-rest: PostgreSQL default |

## Data Flow

1. **Ingress**: Zerodha KiteTicker WebSocket → validated ticks → TimescaleDB
2. **Processing**: Ticks → candles → indicators → signals → orders (all in-memory, persisted to DB)
3. **Egress**: Telegram notifications (trade alerts, system status), session report CLI output
4. **No external data sharing**: TradeOS does not send trading data to any third party

## Secrets Inventory

| Secret | Location | Purpose |
|--------|----------|---------|
| Zerodha API key | `config/secrets.yaml` | KiteConnect authentication |
| Zerodha API secret | `config/secrets.yaml` | Token exchange |
| Zerodha access token | `config/secrets.yaml` | Daily session auth (refreshed daily) |
| Telegram bot token | `config/secrets.yaml` | Alert notifications |
| Telegram chat ID | `config/secrets.yaml` | Alert channel targeting |

## Compliance Notes

- **GDPR/DPDPA**: Not applicable — no personal data processed
- **Financial regulations**: Paper trading only (no real orders). Live mode requires explicit operator action.
- **Data deletion**: Database can be dropped and recreated. Logs auto-rotate after 90 days.
