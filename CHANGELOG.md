# Changelog

All notable changes to TradeOS are documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.5.0] — 2026-03-16

### Added
- OSD v1.9.0 compliance audit — CHANGELOG, data inventory, infrastructure doc, rollback runbook, secrets template
- B15 max positions race condition fix — defense-in-depth (pending counter + hard gate + capital ceiling)
- Flaky test fix — `test_callback_valid_token` stabilized, B15 test warnings resolved

### Changed
- ASPS v1.0.0 restructure — engine modules moved to `core/`, subdirectory CLAUDE.md files, root CLAUDE.md rewritten (<200 lines)
- ADRs, runbooks, and strategy specs directories created under `docs/`
- Report: Capital + Charges columns with Indian number formatting (₹X,XX,XXX)
- Report: UTC→IST timestamp conversion for all displayed times
- Report: fixed-width table columns, ANSI color for P&L, verify mode gross-vs-gross comparison

### Fixed
- B15: max positions race condition — 6 positions opened with max=4 due to async fill delay
- Nginx `proxy_pass` — docker0 bridge unreachable, switched to VPS public IP
- Cron timing — VPS clock runs IST, corrected UTC-converted entries
- Report formatting — table alignment, HH:MM:SS timestamps

## [0.4.0] — 2026-03-14

### Added
- DB trade history — signals table dual-write (PENDING/FILLED/REJECTED/IGNORED/KILL_SWITCHED), sessions table with EOD summary, backfill script for Session 07
- Token automation — Nginx + Let's Encrypt (port 11443), callback server with auto-start, 4-stage Telegram escalation
- `tradeos` CLI v0.2.0 — 25+ subcommands, color-coded output, preflight check, auto-report
- Production logging — date-based log files (`logs/{module}/{module}_{date}.log`)
- Log rotation — 30-day compress, 90-day delete, weekly cron
- Session report — DB+verify modes, CSV/XLSX export
- Codebase audit — 2 critical fixes (signal_id chain, structlog field names), 5 warnings resolved

### Changed
- Tests: 439 → 499

## [0.3.0] — 2026-03-13

### Added
- HAWK AI Market Intelligence Engine — multi-model consensus (Claude, Gemini, GPT-5.4, Kimi), evening + morning runs, JSON + Telegram output, eval scorer
- Rich Telegram notifications — 6 event types + heartbeat summary, config-driven via `telegram_alerts.yaml`
- Session report CLI — structlog parser, signal/trade/P&L/regime/health tables

### Fixed
- B7: SHORT position phantom P&L (-₹199,679) — field name mismatch (`avg_price` vs `entry_price`)
- B8: ghost positions from exit fills
- B9: report parser hardened for edge cases
- B10: pre-market log noise gated
- B11: single regime detector init
- B12: gross P&L computation corrected
- B13: Telegram field names corrected
- B14: hard exit labeled properly

### Changed
- `resolve_position_fields()` utility eliminates field name bugs permanently
- Tests: 303 → 439

## [0.2.0] — 2026-03-11

### Added
- Slot-based position sizing — 3-layer calculation (risk-based → capital cap → viability floors)
- No-entry window Gate 5b at 14:30 IST (configurable)
- Min slot capital ₹40K startup validation
- Pending order cancellation at hard_exit before emergency_exit_all
- Config extraction — all 10 S1 strategy parameters to `config/settings.yaml`
- Paper capital increased ₹5L → ₹10L
- Option C stop floor — minimum 2% stop distance

### Fixed
- B1–B6: Session 03 bugs (6 total — 2 critical, 3 high, 1 medium)

### Changed
- Tests: 100 → 303

## [0.1.0] — 2026-03-06

### Added
- Initial TradeOS system — S1 Intraday Momentum strategy
- Data engine: KiteTicker WebSocket → tick validation → candle builder
- Strategy engine: EMA9/21 crossover + RSI + VWAP + volume ratio signal generation
- Risk gate: 7-gate pipeline (kill switch, recon, instrument lock, max positions, time windows, dedup, regime)
- Execution engine: 8-state order state machine, paper order placer
- Risk manager: kill switch (3 levels), position sizer, P&L tracker, loss tracker
- Regime detector: 4-regime classifier (BULL_TREND, BEAR_TREND, HIGH_VOLATILITY, CRASH)
- Session lifecycle: pre-market gate → startup → 5 concurrent async tasks → EOD
- TimescaleDB: tick storage, candle storage, signal/trade/session tables
- Telegram notifications: trading channel alerts
- 20-stock NSE watchlist in `config/settings.yaml`
- Paper Sessions 01–03: VWAP bug fix, signal pipeline validation, first live signals
