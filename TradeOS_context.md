# TradeOS Context — Living Document

> Single source of truth. Updated at session end or after major changes.

---

## 1. Project Overview

TradeOS — AI-powered systematic trading system for NSE intraday equities.
Repo: `arushai-hq/tradeOS` | Infra: Rocky Linux 9.7 VPS | Broker: Zerodha via `pykiteconnect v5` | Exchange: NSE (equities, MIS intraday only)

---

## 2. Architecture

Engine modules live under `core/` (ASPS Pattern B structure):

| Module | Role |
|--------|------|
| `core/data_engine/feed.py` | KiteTicker WebSocket → asyncio queue bridge |
| `core/data_engine/validator.py` | 5-gate tick filter (price, circuit, volume, staleness, dedup) |
| `core/strategy_engine/candle_builder.py` | Tick → 15-min OHLCV+VWAP candles, one instance per instrument |
| `core/strategy_engine/indicators.py` | EMA9/21, RSI, VWAP, volume ratio, swing high/low |
| `core/strategy_engine/signal_generator.py` | S1 signal evaluation (LONG/SHORT) + session dedup |
| `core/strategy_engine/risk_gate.py` | Gates 1–7; Gate 7 = regime check (blocks counter-trend signals) |
| `core/regime_detector/` | 4-regime classifier: BULL_TREND / BEAR_TREND / HIGH_VOLATILITY / CRASH |
| `core/risk_manager/` | Kill switch (D1), position sizer, PnL tracker, loss tracker |
| `core/execution_engine/` | Order state machine (8 states), paper order placer |
| `main.py` | D9 session lifecycle: pre-market gate → startup → 5 concurrent tasks → EOD |
| `utils/telegram_notifier.py` | Rich Telegram alerts — 6 event types + heartbeat summary. Config-driven via `config/telegram_alerts.yaml` (hot-reload, 60s TTL) |
| `tools/session_report.py` | Standalone CLI session report — parses structlog, outputs signal/trade/P&L/regime/health tables. Supports `--export csv/xlsx/all` |
| `core/risk_manager/position_sizer.py` | 3-layer slot-based sizing: risk-based → capital cap → viability floors (min_risk ₹1K, min_value ₹15K) |
| `tools/hawk_engine/` | HAWK AI Market Intelligence Engine. Multi-model consensus (4 LLMs via OpenRouter). Evening + morning runs. Data: KiteConnect (primary) → nsetools/nsepython (fallback). Shared KiteConnect instance per run. Output: JSON + Telegram. Eval scorer: `tools/hawk_eval.py`. |
| `migrations/` | SQL migration files. `001_create_sessions_table.sql`, `002_backtest_tables.sql`. Auto-created at startup if missing. |
| `tools/data_downloader.py` | Historical data downloader. KiteConnect candles → backtest_candles DB. 5 intervals (5min, 15min, 30min, 1hour, day). Resume, rate limiting, ON CONFLICT idempotent. CLI: `tradeos data download/status`. |
| `tools/db_backfill_session07.py` | One-time data fix for Session 07 trades (incorrect P&L + exit_reason from pre-B12 code). |
| `scripts/token_server.py` | HTTP callback server (0.0.0.0:7291). Captures Zerodha request_token, exchanges for access_token, writes to secrets.yaml, confirms via Telegram, auto-shuts down. Auto-starts main.py in named tmux session (weekdays only). |
| `scripts/token_cron.py` | Daily cron orchestrator. Starts token_server, sends login URL to Telegram, 4-stage escalation (07:00, 07:30, 08:00, 08:30 IST), kills server at 08:45 if no auth. |
| `scripts/log_rotation.py` | Log rotation: compress after 30 days, delete after 90 days. Runs daily via cron at 02:00 IST. Configurable via settings.yaml `log_rotation` section. |
| `docker/nginx/` | Nginx reverse proxy (SSL on port 11443). Proxies /callback to token_server. Let's Encrypt cert via certbot. Port 80 for cert renewal only. |
| `bin/tradeos` | Unified CLI entry point (v0.2.0). Bash shim with color-coded output. 25+ subcommands: auth, start/stop/restart, status, preflight, report (+ auto), hawk, logs, db, docker, config, cron, test, version. Symlinked to `/usr/local/bin/tradeos` via `scripts/install_tradeos_cli.sh`. |
| `core/CLAUDE.md` | Engine skill router — D1-D9 disciplines, conventions, gotchas |
| `tools/CLAUDE.md` | CLI tools and HAWK AI skill router |
| `tests/CLAUDE.md` | Test suite conventions and commands |
| `docs/decisions/` | Architecture Decision Records (ADR-001: position sizing, ADR-002: token automation) |
| `docs/runbooks/` | Operational procedures (daily-trading.md) |

**Strategy (current):** S1 Intraday Momentum — EMA9/21 crossover + VWAP + RSI + volume ratio. **DEPRECATED** — negative expectancy confirmed via backtester across all parameter combinations. Kept running for infrastructure validation only.
**Strategy (in development):** S1v2 Trend Pullback (Raschke/Schwartz/PTJ hybrid) + S1v3 Mean Reversion (Kotegawa-inspired). Full spec: `docs/strategy_specs/strategy_spec_s1v2_s1v3.md`
**Watchlist:** 50 NIFTY 50 stocks in `config/settings.yaml` (expanded 2026-03-16)
**Candle interval:** 15 minutes | **Trade window:** 09:15–15:00 IST | **Hard exit:** 15:00 IST

---

## 3. Current State

| Item | Status |
|------|--------|
| Tests | **640 passing, 0 failures, 12 skipped** |
| Capital | Paper trading capital: ₹10,00,000. Slot capital: ₹1,50,000. Risk/trade: ₹2,250. |
| S1 allocation | 90% (₹9,00,000). Max positions: 6. S2=5%, S3=3%, S4=2%. |
| S1 config | All S1 strategy parameters extracted to config/settings.yaml (10 params). Current tuned values: volume_ratio_min 1.2, no_entry_after 14:45, min_stop_pct 0.02. Stop floor at 2% prevents sizer rejection on tight swing stops. |
| Paper Session 05 | Complete — system health PASS. Zero bugs, zero false kill switch, zero ghost positions. B7-B11 fixes confirmed. 6 signals (3 accepted, 3 blocked by no-entry window). Zero trades — all 3 accepted signals rejected by position sizer due to tight swing stops (pre-fix). Stop floor + ₹10L capital fix applied post-session. |
| Paper Session 06 | Config tuning: volume_ratio_min 1.5→1.2, no_entry_after 14:30→14:45. T1-T3 Telegram fixes live. Validates S1 with ₹10L capital + 2% stop floor + widened params. |
| Paper Session 07 | **FIRST REAL TRADES.** 2 trades (SUNPHARMA SHORT +₹1,361 net, TITAN SHORT +₹30 net). Both held to 15:00 hard exit. Actual session P&L: +₹1,390 net. System stable — kill switch Level 0, no ghosts. |
| B12-B14 fixes | gross P&L now computed correctly, Telegram shows correct fields, hard exit labeled properly. `resolve_position_fields()` utility eliminates field name bugs permanently (`af8a007`). |
| HAWK status | feature/hawk merged into main (`094e04a`). Codebase unified — S1 trading + HAWK AI engine on single branch. |
| HAWK eval | Day 2: 42.1% overall BUT 100% on SHORT picks (8/8). Cumulative: SHORT picks 19/19 (100%). Day 3 consensus: 8 unanimous ALL SHORT. |
| Session 03 bugs | **All 6 resolved (B1–B6).** System is Session 04 ready. |
| New tooling | Rich Telegram notifications (`cdd066b`) and session report CLI (`4559b7a`) |
| Bear regime signal insight | Session 03 re-analysis: all 3 accepted signals were oversold SHORTs (now blocked by B3 fix). In bear_trend, LONGs blocked by Gate 7 + SHORTs blocked by B3 RSI filter = potential zero-signal sessions. Monitor in Session 04. |
| Slot-based position sizing | 3-layer calculation implemented (`361876e`). No-entry window at 14:30 IST (`c60648f`). Min slot capital ₹40K startup validation + pending order cancellation at hard_exit (`c862313`). |
| DB trade history | D1 signal status updates (FILLED/REJECTED) wired. D3 sessions table created with EOD write. D4 backfill script ready. D5 dead code removed from storage.py. On feature/db-trade-history — pending VPS deploy + merge. |
| Token automation | Token automation complete. Nginx + Let's Encrypt (port 11443). Callback server captures token, auto-starts main.py in tmux (weekdays). 4-stage Telegram escalation. Config-driven timing. Date-based production logging: `logs/tradeos/tradeos_{date}.log`, `logs/hawk/hawk_{date}.log`, `logs/token/token_{date}.log`. Log rotation: 30-day compress, 90-day delete. |
| tradeos CLI | v0.2.0 — 25+ subcommands, color-coded output, preflight check, auto-report. Installed at `/usr/local/bin/tradeos`. |
| context-mode | MCP plugin (mksglu/context-mode v1.0.22) for context window optimization and session continuity. Sandboxes raw data out of context via SQLite + FTS5/BM25 indexing. Use `--continue` flag when resuming sessions to carry forward indexed context. Hooks intercept curl/wget and route through `ctx_execute`/`ctx_fetch_and_index`. |
| OSD v1.9.0 Compliance | **2026-03-16** — Full 29-standard audit complete. Gaps filled: CHANGELOG.md, data inventory, infrastructure register, rollback runbook, secrets template, git tag v0.5.0. Result: 15/29 PASS, 12/29 PARTIAL (acceptable), 2/29 N/A. |
| OSD Skills Audit | **2026-03-15** — Skills audited and enhanced. 4 new skills created (tradeos-architecture, tradeos-gotchas, tradeos-testing, tradeos-operations). CLAUDE.md verified against OSD Section 4.2 — deployment rule, branch discipline, and skills reference added. All 13 TradeOS skills operational. context-mode routing block verified intact. |
| B15 fix | **2026-03-16** — Max positions race condition fixed. Defense-in-depth: Layer 1 (pending_signals counter in Gate 4), Layer 2 (hard gate in execution engine), Layer 3 (capital ceiling check). Session 08 scenario (5 simultaneous signals with 1 open) now correctly limited to 3 new positions. Tests: 515→523. |
| ASPS Restructure | **2026-03-15** — ASPS v1.0.0 restructure complete. Pattern B (Engine + Tools), HEAVY tier. Engine modules moved to `core/` (data_engine, strategy_engine, execution_engine, risk_manager, regime_detector). Subdirectory CLAUDE.md files for skill routing. Root CLAUDE.md rewritten (<200 lines). ADRs, runbooks, and specs directories created. Tests: 499 passed. Branch: `refactor/asps-restructure`. |
| Backtester | Operational. 2.75M candles (52 symbols × 5 intervals). First runs complete. S1 fixed/trailing/partial all show negative expectancy. Parameter optimization confirms no profitable configuration exists for current S1 entry logic. |
| Strategy redesign | S1v2 (trend pullback) + S1v3 (mean reversion) spec locked in TradeOS-03. Full spec: `docs/strategy_specs/strategy_spec_s1v2_s1v3.md`. Pending backtester implementation. |
| Mode | `paper` — never change to `live` without explicit instruction |
| Active strategy | S1 (deprecated). S1v2 (killed — both timeframes failed). S1v3 in backtester development. |
| Paper Session 01 | Complete — VWAP bug found and fixed |
| Paper Session 02 | Complete — signal pipeline validated |
| Paper Session 03 | Complete — debrief complete. 9 signals generated, 3 converted to positions. 6 bugs found (2 critical, 3 high, 1 medium). Zero P&L tracked — tracker bug. First session with live signal generation and position entry. |
| Paper Session 04 | Complete — debrief complete. 2 trades taken (LT SHORT, AXISBANK SHORT), both killed after 30 seconds by false kill switch. Gross P&L: ₹0. Net P&L: -₹239 (charges only). Kill switch Level 2 for entire day due to phantom -₹199,679 unrealized P&L. 2 critical bugs found (B7, B8). |
| Position sizing validation | Slot-based sizing working correctly — LT qty=51, AXISBANK qty=155 match slot capital calculations. |
| B7+B8 fixes | **Both critical Session 04 bugs fixed.** Unrealized P&L now correct for SHORTs (`cc9c018`). No ghost positions from exit fills (`7ed6b7a`). Session 05 ready. |
| All Session 04 bugs | **All 5 resolved (B7–B11).** B9 report parser hardened, B10 pre-market logs gated, B11 single regime init. System is Session 05 ready. |

---

## 4. Key Decisions

1. **Depth before breadth** — Validate S1 fully before building S2.
2. **Data-gated trailing stop** — Design spec complete (`docs/strategy_specs/trailing_stop_spec.md`). Blocked until ≥ 5 trades past 2R target. First review: **2026-03-16**.
3. **Dashboard deferred** — Build after 3–4 sessions with real P&L data. No premature UI.
4. **Cosmetic DB bugs tolerated** — `tradingsymbol`, `bid`, `ask` fields null in tick storage. Zero impact on signal generation or risk logic.
5. **AI/LLM dynamic watchlist parked** — Revisit after S1 validated on fixed watchlist.
6. **Accept zero-signal sessions as valid outcome** — S1 sitting out unfavorable regimes is correct behavior. Do not loosen gates to force trades. Revisit only if zero-signal persists across 3+ sessions with mixed regimes.
7. **Nemawashi Principle** — *"Preparing the roots before transplanting the tree."* All features, fixes, and system changes follow a 70-80% planning / 20-30% implementation split. Deep-dive analysis, edge case mapping, cost modeling, and brainstorming MUST be completed before any CC prompt is generated. No rushing to implementation. This applies to every session, every feature, every decision.
8. **Capital config** — S1=90%, 6 slots (₹1,50,000/slot). S2/S3/S4 placeholders at 5%/3%/2%. Increased from 70%/4 for Session 09 to allow more concurrent positions during validated paper trading.
9. **Slot-based position sizing** — 3-layer calculation: risk-based shares → capital cap scale-down → viability floors (min_risk ₹1,000, min_position_value ₹15,000). Charge estimation logged per sized position. No-entry window at 14:45 IST (Gate 5b). Startup refuses if slot_capital < ₹40,000. Pending orders cancelled at hard_exit before emergency_exit_all.
10. **Futures trading gate criteria** — No futures until ALL conditions met: (a) 10 completed S1 trades (stop/target/hard_exit, not just opened), (b) 3 consecutive bug-free sessions, (c) every trade P&L verified in session report matches expected calculation, (d) at least 1 winning trade proving strategy can make money. Manual delivery trades (NIFTY BEES, large-caps) are acceptable anytime for market views — separate from TradeOS.
11. **HAWK** — AI watchlist engine. Standalone shadow-testing tool. KiteConnect primary → nsetools/nsepython fallback. Shared kite instance per run. Claude Sonnet. Dual storage: JSON + TimescaleDB. Separate Telegram channel (HAWK-Picks). Full spec: `docs/hawk_spec.md`. TATAMOTORS demerged → TMPV is NIFTY 50 constituent.
12. **Option C stop floor** — Minimum 2% stop distance enforced when swing-based stops are tighter. Paper capital increased ₹5L→₹10L for realistic testing. All 10 S1 strategy parameters (EMA periods, RSI thresholds, volume ratio, RR ratio, swing lookback, min stop %) now configurable via settings.yaml. Zero hardcoded numbers in signal generation.
13. **HAWK multi-model consensus** — 4 LLMs (Claude, Gemini, GPT-5.4, Kimi) run on same data. Picks scored: UNANIMOUS (4/4), STRONG (3/4), MAJORITY (2/4), SINGLE (1/4). $0.23/run, ~$10/month. All 4 models selected after side-by-side comparison — 80% pick overlap confirmed.
14. **S1 strategy validation failed backtesting** — Negative expectancy across all exit modes and parameter sweeps (Jan 2025 - Mar 2026). Entry logic (EMA crossover + RSI + VWAP + volume) generates too many false signals. Architectural redesign required before live capital deployment. Paper trading continues for infrastructure validation.
15. **S1v2 Trend Pullback design** — Raschke "Holy Grail" + Schwartz 10-EMA filter + PTJ risk management. Multi-timeframe: 15min trend filter (10-EMA direction + ADX>25), 5min entry (first pullback to 20-EMA then reclaim, volume>1.5×avg). Target: 2.5×ATR. Stop: pullback low/high. R:R gate: minimum 3:1. Time stop: 150min. Full spec: `docs/strategy_specs/strategy_spec_s1v2_s1v3.md`.
16. **S1v3 Mean Reversion design** — Kotegawa-inspired panic buy + Bollinger Band oversold + VWAP target. 15min chart only. Trigger: stock drops >2×ATR from day high, RSI<30, price at/below lower BB(20,2), first green reversal candle with volume>1.5×avg. Target: VWAP. Stop: intraday low. R:R gate: minimum 2:1. Valid window: 09:30–14:30 IST. Both LONG and SHORT directions.
17. **Backtester strategy implementation** — Two separate strategy classes (s1v2_trend_pullback.py, s1v3_mean_reversion.py). New indicators required: EMA(10), EMA(20), ADX(14), Bollinger Bands(20,2). Shared position pool (6 max). Success criteria: positive P&L, win rate ≥40%, profit factor ≥1.3, max drawdown ≤15%, avg R:R ≥2.0, minimum 50 trades.
18. **Strategy research methodology** — Derived from studying 7 proven short-term traders. Key principles extracted: (a) enter on pullback/reversal not exhausted momentum, (b) asymmetric R:R minimum 3:1, (c) regime awareness — don't trade choppy markets, (d) fast exits via price stop + time stop, (e) directional filter — never trade against prevailing trend.
19. **S1v2 ATR stop floor** — Stop = wider of (pullback low/high, entry ± 1.0×ATR). Prevents position sizer rejection from tight 5min pullback stops. R:R gate recalculates with wider stop. Config: `strategy.s1v2.atr_stop_floor_mult: 1.0`.
20. **Backtester min_risk_floor override** — Live position sizer uses ₹1,000 min_risk_floor. On 5min NIFTY 50 stocks, per-trade risk is ₹200-400 — always below ₹1,000. Backtester overrides to ₹200 (covers round-trip commission ₹40) via `config/settings.yaml` → `backtester.min_risk_floor: 200`. No `core/` changes — override passed as kwarg to `position_sizer.calculate()`.
21. **S1v2 5min timeframe failed** — 108 trades, -₹36,158, 18.5% win rate, profit factor 0.23. 5min ATR too small for NIFTY 50 large-caps. Targets unreachable before stops hit. Switching to 15min single-timeframe mode.
22. **S1v2 15min single-timeframe mode** — All indicators and entries on 15min. Config: `strategy.s1v2.timeframe_mode: single`. Time stop: 20 bars × 15min = 300min. Multi-TF mode preserved via `timeframe_mode: multi`.
23. **S1v2 killed** — Both 5min (run #9: 108 trades, 18.5% WR, -₹36,158) and 15min (run #10: 12 trades, 0% WR, -₹6,900) failed. EMA pullback + ADX trend filter does not produce viable signals on intraday NSE equities. Strategy abandoned.
24. **S1v3 Mean Reversion implementation** — Kotegawa-inspired panic buy + BB oversold + VWAP target. 15min single-timeframe. All parameters from config. Fixed VWAP target at entry. Reversal timeout 5 bars. Min R:R 2.0.
25. **S1v3 configurable interval** — S1v3 on 15min produced 79 trades with 0% WR (100% EOD exits). Price dips but never reverts to VWAP by close on 15min. Added `strategy.s1v3.interval` config (`"5min"` or `"15min"`, default `"5min"`). Backtester loads candles and warmup at configured interval. No strategy logic changes — just data granularity.

---

## 5. Completed Work

| Item | Detail |
|------|--------|
| T1 instrument subscription audit | Clean — `kite.subscribe()` + `MODE_FULL` verified in `feed.py` |
| Regime detector | 10 commits; production-integrated in `StrategyEngine`, `RiskGate` Gate 7, `risk_watchdog` 60s refresh |
| Trailing stop design spec | `docs/strategy_specs/trailing_stop_spec.md` — pending data gate |
| VWAP field fix | `average_price` → `average_traded_price` (pykiteconnect v5 field name) |
| EOD CancelledError fix | Clean `sys.exit(0)` at 15:30; `CancelledError` no longer propagates |
| Gate 4 timezone fix | `zoneinfo` + `.astimezone(IST)` — VPS UTC offset no longer causes false staleness |
| Gate 4 threshold | 30s — `exchange_timestamp` = last trade time (not delivery); illiquid stocks need headroom |
| Tick queue fan-out | Two queues: `tick_queue_storage` (raw) → DataEngine; `tick_queue_strategy` (validated) → StrategyEngine |
| Debug logging | `candle_built` + `signal_evaluated` debug events added |
| Slot-based position sizing | 3-layer sizing (`361876e`), no-entry window Gate 5b at 14:30 IST (`c60648f`), min slot capital ₹40K startup validation + pending order cancellation at hard_exit (`c862313`). Tests: 303→318. |

---

## 6. Known TODOs

| Bug | Impact | Priority |
|-----|--------|----------|
| `tradingsymbol` null in `ticks` table | Cosmetic — token present, symbol lookup works | Low |
| `bid` / `ask` null in `ticks` table | Cosmetic — not used in S1 logic | Low |
| ~~HAWK: nsepython bhavcopy/FII-DII broken~~ | **RESOLVED** — KiteConnect primary, nsepython enrichment for delivery %. FII/DII graceful zeros when nse_fii removed. | ~~Medium~~ |
| HAWK: Telegram config for HAWK-Picks channel | bot_token + chat_id not yet configured in secrets.yaml | Medium |
| HAWK: Regime shows "unknown" | Needs TradeOS regime integration | Low |
| D2: System events expansion | SESSION_START, SESSION_END, KILL_SWITCH, REGIME_CHANGE, HARD_EXIT not yet captured | Medium |
| HAWK: evaluator shows "?" for consensus conviction | Evaluator not parsing consensus conviction field | Medium |
| HAWK: morning run looks for today's evening file | Should look for yesterday's evening file | Medium |

---

## 7. Deferred Roadmap

- **Trailing stop** — Design spec complete (`docs/strategy_specs/trailing_stop_spec.md`). Data-gated: blocked until ≥ 5 trades past 2R target. First review: **2026-03-16**.
- **S2 multi-regime short strategy** — BEAR_TREND / HIGH_VOLATILITY regime-aware entries. Allocation: 15%.
- **S3 positional strategy** — Multi-day swing trades. Allocation placeholder: 10%. Design pending S1 validation.
- **S4 event strategy** — Earnings/macro event-driven trades. Allocation placeholder: 5%. Design pending S2.
- **Admin dashboard** — Mobile/iPad SaaS; session P&L, signal log, regime status. Build after 3–4 sessions.
- **Futures paper trading** — NIFTY futures alongside S1. Gated on: 10 completed S1 trades + 3 clean sessions + verified P&L + 1 winner. Infrastructure needed: lot-aware position sizer, expiry management, margin monitoring. Design (Nemawashi) can begin during S1 validation phase — no code until gates clear.
- **Commodities** — Deferred until futures infrastructure built and validated.
- **Production readiness** — Phased: (1) DB + token automation + log rotation + systemd, (2) Docker Compose + FastAPI + basic WebUI, (3) encrypted secrets + VPS hardening + monitoring, (4) full WebUI + HAWK UI + mobile. Phase 1 progress — DB trade history complete, token automation complete, production logging complete, log rotation complete, tradeos CLI complete. Remaining Phase 1: systemd service (optional — tmux auto-start working).
- **`tradeos` CLI packaging roadmap** — Phase A: bash shim with subcommands (done). Phase B: Python Click CLI with argument validation. Phase C: `pyproject.toml` with `console_scripts` entry point. Phase D: systemd service files generated by CLI. Phase E: RPM/DEB packaging. Phase F: universal installer script (`curl | bash`).

---

## 8. Immediate Next Actions

1. **S1v2 backtester implementation** — Implement s1v2_trend_pullback strategy class in backtester. Add ADX, Bollinger Bands, EMA10, EMA20 indicators. Test against 198-day dataset. CC prompt ready.
2. **S1v3 backtester implementation** — Implement s1v3_mean_reversion strategy class in backtester. Test against 198-day dataset. CC prompt ready.
3. **Strategy comparison** — Run S1 vs S1v2 vs S1v3 comparison. Apply success criteria. Deploy winner to paper trading.
4. **Continue paper trading** — S1 continues for infrastructure validation. Session 10 on next trading day.
5. **Token automation verification** — PYTHONPATH fix deployed, first real cron test pending.
6. Futures gate: 10/10 trades ✓, 2/3 clean sessions, P&L verified ✓, 1+ winner ✓ — 3/4 gates met.

---

## 9. Session Log

| Date | Session | Summary | Outcome |
|------|---------|---------|---------|
| 2026-03-17 | Backtester Data Infrastructure | 4 DB tables (backtest_candles, backtest_metadata, backtest_runs, backtest_trades) in `migrations/002_backtest_tables.sql`. Auto-create at startup. `tools/data_downloader.py`: KiteConnect historical candle downloader with 5 intervals, resume, rate limiting, ON CONFLICT idempotent. CLI: `tradeos data download/status`. PYTHONPATH fix for CLI/cron. Tests: 551. | Data layer ready for backtester engine. |
| 2026-03-17 | Core Backtester Engine | `tools/backtester.py`: replays historical candles through exact S1 pipeline (IndicatorEngine, SignalGenerator, PositionSizer, ChargeCalculator, classify_regime). BacktestRiskGate adapts Gates 4-7 with candle_time. Three exit modes: fixed, trailing (ATR), partial (50% at 1R). Optimizer (param sweep), compare (exit mode comparison). DB storage + rich terminal report. CLI: `tradeos backtest run/optimize/compare/show`. Tests: 576. | Backtester complete. Ready for live data download + first run. |
| 2026-03-17 | Backtester VWAP Fix | KiteConnect historical_data returns OHLCV but not VWAP. Backtester was setting `vwap=close`, making `close > vwap` always False — zero signals. Fix: `_compute_vwap_for_day()` computes running VWAP per-stock per-day from `(H+L+C)/3 × volume`. Resets each day. No live code changes. Tests: 577. | Backtester now generates signals correctly. |
| 2026-03-17 | Session 09 + Backtester | Session 09: 5 trades (1W/4L), -₹3,196 net, 36 signals (31 regime-blocked). Backtester: 2.75M candles downloaded, first full backtest run. S1 loses money across ALL parameter combinations — fixed exits (-₹1.16L/51d), trailing (-₹1.85L), partial (-₹1.83L). ATR sweep 1.0-4.0× all negative. RSI sweep 40-65 all negative. Volume ratio sweep pending. Signal quality is the core issue, not exit strategy. | Critical finding — S1 needs architectural redesign before live trading. |
| 2026-03-17 | TradeOS-03 Strategy Redesign | Researched 7 proven short-term traders. Designed S1v2 (trend pullback) + S1v3 (mean reversion). 18 decisions locked. Spec: `docs/strategy_specs/strategy_spec_s1v2_s1v3.md`. | Spec locked. CC prompts for backtester implementation ready. |

---

## 10. Session Rules

These rules apply to every TradeOS session regardless of context window or session reset.

1. **Nemawashi First** — No CC prompt is generated until planning is complete. Every feature goes through: problem definition → research → brainstorm → edge case mapping → cost/risk analysis → decision lock → THEN implementation. Ratio: 70-80% planning, 20-30% implementation.
2. **Living Document Protocol** — Every conclusion, decision, or significant discussion must be captured in `TradeOS_context.md` via a CC delta prompt before moving to the next topic.
3. **Context Handoff** — If a session approaches context limits, generate a handoff document and update `TradeOS_context.md` with the exact resume point before the session ends.
4. **Allocation Sum Rule** — All strategy allocations in `config/settings.yaml` MUST sum to 1.00. Validated at startup — system refuses to start if violated. Any config change to one allocation requires adjusting others to maintain sum.
5. **Position Sizing & Max Positions** — `min_slot_capital` (₹40,000), `min_risk_floor` (₹1,000), `min_position_value` (₹15,000) are configured in `config/settings.yaml` under `position_sizing`. `no_entry_after` (14:30 IST) is under `trading_hours`. All are startup-validated or gate-checked — never bypassed at runtime. **B15 defense-in-depth**: pending_signals counter (Gate 4) + hard gate (execution engine) + capital ceiling prevent race conditions when multiple signals arrive from the same candle batch.
6. **Context Hygiene** — `TradeOS_context.md` is a rolling window, not a history book. Rules: (a) Known TODOs: Only OPEN items stay. Completed items move to `docs/context_archive.md` after 2 sessions. (b) Session Log: Keep last 5 sessions only. Older rows move to `docs/context_archive.md`. (c) Completed Work: Summarize, don't accumulate. Move details to archive when section exceeds 10 items. (d) Key Decisions and Session Rules: Stay in main file permanently (compact, always relevant). (e) Archive file is append-only — never edit or delete archived content.
7. **Telegram Channel Separation** — Each engine/module gets its own Telegram channel. Never mix notification streams. Current channels: TradeOS-Trading (S1 signals, fills, exits, heartbeat, system), HAWK-Picks (AI watchlist). New modules must define their own channel before implementation.
8. **Git Branching** — feature/* for new features, fix/* for bugs, main = production (deployed on VPS). Feature branches created from main, kept in sync with `git rebase main`. Merge to main only when fully tested and all tests pass. CC must track which branch is for which feature. Never develop new features directly on main. Current branches: main (S1 trading + HAWK AI engine, production).
9. **SHORT Position Accounting** — Negative qty for shorts. Field names: `avg_price` (not `entry_price`), `side` (not `direction`). This mismatch caused B7 (false kill switch with -₹199,679 phantom P&L). Always verify field names when accessing position data.
10. **Log File Convention** — All modules write date-based log files: `logs/{module}/{module}_{YYYY-MM-DD}.log`. Subdirectories: `tradeos/` (main trading), `hawk/` (AI engine), `token/` (auth). Log rotation compresses files >30 days, deletes >90 days. session_report.py accepts any log file path. Never use `paper_session_NN.log` naming — always date-based.
11. **CLI Convention** — All operations go through `tradeos <command>` in production. Never call Python scripts directly. New scripts must be registered as tradeos subcommands before deployment.
12. **Documentation Convention** — README.md and CLAUDE.md must be updated with every feature addition. Every CC prompt must include README.md update requirements. CLAUDE.md contains all session rules and CC conventions for project continuity.

---

## 11. Last Updated

**2026-03-17** — S1v3 configurable interval added (CC007). Default 5min for faster reversion capture. S1v3 15min run showed 79 trades, 0% WR. Tests: 640 passing.
