-- TradeOS Migration 003: Futures backtest data tables
-- Historical futures candle data for NIFTY/BANKNIFTY backtesting.
--
-- v2: Added tradingsymbol + expiry columns for per-contract intraday data.
--     Daily continuous candles: tradingsymbol='', expiry=NULL.
--     Intraday per-contract: tradingsymbol='NIFTY26MARFUT', expiry=2026-03-27.
--
-- Apply:
--   psql -U tradeos -d tradeos -f migrations/003_futures_backtest_tables.sql
-- Or:
--   docker exec -it tradeos-db psql -U tradeos -d tradeos -f /migrations/003_futures_backtest_tables.sql

-- 1. backtest_futures_candles — Historical OHLCV+OI candles for index futures
CREATE TABLE IF NOT EXISTS backtest_futures_candles (
    instrument         TEXT           NOT NULL,
    tradingsymbol      TEXT           NOT NULL DEFAULT '',
    expiry             DATE,
    interval           TEXT           NOT NULL,
    timestamp          TIMESTAMPTZ    NOT NULL,
    open               NUMERIC(12,2)  NOT NULL,
    high               NUMERIC(12,2)  NOT NULL,
    low                NUMERIC(12,2)  NOT NULL,
    close              NUMERIC(12,2)  NOT NULL,
    volume             BIGINT         NOT NULL,
    oi                 BIGINT,

    PRIMARY KEY (instrument, tradingsymbol, interval, timestamp),

    CONSTRAINT chk_fut_instrument CHECK (
        instrument IN ('NIFTY', 'BANKNIFTY')
    ),
    CONSTRAINT chk_fut_interval CHECK (
        interval IN ('5min', '15min', 'day')
    )
);

-- 2. backtest_futures_metadata — Download progress per instrument + interval
CREATE TABLE IF NOT EXISTS backtest_futures_metadata (
    instrument         TEXT           NOT NULL,
    interval           TEXT           NOT NULL,
    first_candle       TIMESTAMPTZ,
    last_candle        TIMESTAMPTZ,
    candle_count       INTEGER        DEFAULT 0,
    lot_size           INTEGER,
    last_download      TIMESTAMPTZ    DEFAULT NOW(),

    PRIMARY KEY (instrument, interval),

    CONSTRAINT chk_futm_instrument CHECK (
        instrument IN ('NIFTY', 'BANKNIFTY')
    )
);
