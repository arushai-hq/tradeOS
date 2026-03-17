-- TradeOS Migration 002: Backtest data tables
-- Historical candle data for backtesting + run tracking.
--
-- Apply:
--   psql -U tradeos -d tradeos -f migrations/002_backtest_tables.sql
-- Or:
--   docker exec -it tradeos-db psql -U tradeos -d tradeos -f /migrations/002_backtest_tables.sql

-- 1. backtest_candles — Historical OHLCV candles downloaded from KiteConnect
CREATE TABLE IF NOT EXISTS backtest_candles (
    instrument_token   INTEGER        NOT NULL,
    symbol             TEXT           NOT NULL,
    interval           TEXT           NOT NULL,
    open               NUMERIC(12,2)  NOT NULL,
    high               NUMERIC(12,2)  NOT NULL,
    low                NUMERIC(12,2)  NOT NULL,
    close              NUMERIC(12,2)  NOT NULL,
    volume             BIGINT         NOT NULL,
    oi                 BIGINT,
    candle_time        TIMESTAMPTZ    NOT NULL,
    session_date       DATE           NOT NULL,

    PRIMARY KEY (instrument_token, interval, candle_time),

    CONSTRAINT chk_interval CHECK (
        interval IN ('5min', '15min', '30min', '1hour', 'day')
    )
);

CREATE INDEX IF NOT EXISTS idx_bt_candles_symbol_interval_time
    ON backtest_candles (symbol, interval, candle_time);

CREATE INDEX IF NOT EXISTS idx_bt_candles_session_interval
    ON backtest_candles (session_date, interval);

-- 2. backtest_metadata — Download progress per symbol + interval
CREATE TABLE IF NOT EXISTS backtest_metadata (
    id                 SERIAL         PRIMARY KEY,
    symbol             TEXT           NOT NULL,
    instrument_token   INTEGER        NOT NULL,
    interval           TEXT           NOT NULL,
    date_from          DATE           NOT NULL,
    date_to            DATE           NOT NULL,
    rows_downloaded    INTEGER,
    downloaded_at      TIMESTAMPTZ    DEFAULT NOW(),

    CONSTRAINT uq_bt_metadata_symbol_interval UNIQUE (symbol, interval)
);

-- 3. backtest_runs — Summary results of each backtest execution
CREATE TABLE IF NOT EXISTS backtest_runs (
    id                 SERIAL         PRIMARY KEY,
    strategy           TEXT           NOT NULL,
    params             JSONB          NOT NULL,
    exit_mode          TEXT,
    date_from          DATE           NOT NULL,
    date_to            DATE           NOT NULL,
    total_trades       INTEGER,
    win_rate           NUMERIC(6,2),
    gross_pnl          NUMERIC(14,2),
    total_charges      NUMERIC(14,2),
    net_pnl            NUMERIC(14,2),
    max_drawdown       NUMERIC(14,2),
    max_drawdown_pct   NUMERIC(8,4),
    sharpe_ratio       NUMERIC(8,4),
    profit_factor      NUMERIC(8,4),
    avg_win            NUMERIC(12,2),
    avg_loss           NUMERIC(12,2),
    expectancy         NUMERIC(12,2),
    created_at         TIMESTAMPTZ    DEFAULT NOW()
);

-- 4. backtest_trades — Detailed trade log per backtest run
CREATE TABLE IF NOT EXISTS backtest_trades (
    id                 SERIAL         PRIMARY KEY,
    run_id             INTEGER        NOT NULL REFERENCES backtest_runs(id),
    symbol             TEXT           NOT NULL,
    direction          TEXT           NOT NULL,
    entry_time         TIMESTAMPTZ,
    exit_time          TIMESTAMPTZ,
    entry_price        NUMERIC(12,2),
    exit_price         NUMERIC(12,2),
    exit_reason        TEXT,
    qty                INTEGER        NOT NULL,
    gross_pnl          NUMERIC(12,2),
    charges            NUMERIC(10,2),
    net_pnl            NUMERIC(12,2),
    regime             TEXT
);

CREATE INDEX IF NOT EXISTS idx_bt_trades_run_symbol
    ON backtest_trades (run_id, symbol);
