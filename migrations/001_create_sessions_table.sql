-- TradeOS Migration 001: Create sessions table
-- Stores one row per trading session with aggregated P&L and health metrics.

CREATE TABLE IF NOT EXISTS sessions (
    session_date       DATE PRIMARY KEY,
    start_time         TIMESTAMPTZ NOT NULL,
    end_time           TIMESTAMPTZ,
    regime             TEXT,
    signals_total      INTEGER DEFAULT 0,
    signals_accepted   INTEGER DEFAULT 0,
    signals_rejected   INTEGER DEFAULT 0,
    trades_total       INTEGER DEFAULT 0,
    trades_won         INTEGER DEFAULT 0,
    trades_lost        INTEGER DEFAULT 0,
    gross_pnl          NUMERIC(12,2) DEFAULT 0,
    total_charges      NUMERIC(10,2) DEFAULT 0,
    net_pnl            NUMERIC(12,2) DEFAULT 0,
    net_pnl_pct        NUMERIC(8,4) DEFAULT 0,
    capital            NUMERIC(14,2) NOT NULL,
    kill_switch_max    INTEGER DEFAULT 0,
    health_status      TEXT DEFAULT 'PASS',
    notes              TEXT
);
