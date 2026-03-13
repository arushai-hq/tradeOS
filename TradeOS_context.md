# TradeOS Context — Living Document

> Single source of truth. Updated at session end or after major changes.

---

## 1. Project Overview

TradeOS — AI-powered systematic trading system for NSE intraday equities.
Repo: `arushai-hq/tradeOS` | Infra: Rocky Linux 9.7 VPS | Broker: Zerodha via `pykiteconnect v5` | Exchange: NSE (equities, MIS intraday only)

---

## 2. Architecture

| Module | Role |
|--------|------|
| `data_engine/feed.py` | KiteTicker WebSocket → asyncio queue bridge |
| `data_engine/validator.py` | 5-gate tick filter (price, circuit, volume, staleness, dedup) |
| `strategy_engine/candle_builder.py` | Tick → 15-min OHLCV+VWAP candles, one instance per instrument |
| `strategy_engine/indicators.py` | EMA9/21, RSI, VWAP, volume ratio, swing high/low |
| `strategy_engine/signal_generator.py` | S1 signal evaluation (LONG/SHORT) + session dedup |
| `strategy_engine/risk_gate.py` | Gates 1–7; Gate 7 = regime check (blocks counter-trend signals) |
| `regime_detector/` | 4-regime classifier: BULL_TREND / BEAR_TREND / HIGH_VOLATILITY / CRASH |
| `risk_manager/` | Kill switch (D1), position sizer, PnL tracker, loss tracker |
| `execution_engine/` | Order state machine (8 states), paper order placer |
| `main.py` | D9 session lifecycle: pre-market gate → startup → 5 concurrent tasks → EOD |
| `utils/telegram_notifier.py` | Rich Telegram alerts — 6 event types + heartbeat summary. Config-driven via `config/telegram_alerts.yaml` (hot-reload, 60s TTL) |
| `tools/session_report.py` | Standalone CLI session report — parses structlog, outputs signal/trade/P&L/regime/health tables. Supports `--export csv/xlsx/all` |
| `risk_manager/position_sizer.py` | 3-layer slot-based sizing: risk-based → capital cap → viability floors (min_risk ₹1K, min_value ₹15K) |
| `hawk_engine/` | HAWK AI Market Intelligence Engine. Multi-model consensus (4 LLMs via OpenRouter). Evening + morning runs. Data: KiteConnect + nsetools. Output: JSON + Telegram. Eval scorer: `tools/hawk_eval.py`. |

**Strategy:** S1 Intraday Momentum — EMA9/21 crossover + VWAP + RSI 55–70 (LONG) / 30–45 (SHORT) + volume ratio ≥ 1.5x
**Watchlist:** 20 hardcoded NSE stocks in `config/settings.yaml`
**Candle interval:** 15 minutes | **Trade window:** 09:15–15:00 IST | **Hard exit:** 15:00 IST

---

## 3. Current State

| Item | Status |
|------|--------|
| Tests | **453 passing, 0 failures, 12 skipped as of commit af8a007** |
| Capital | Paper trading capital: ₹10,00,000. Slot capital: ₹1,75,000. Risk/trade: ₹2,625. |
| S1 allocation | 70% (₹7,00,000). Max positions: 4. S2=15%, S3=10%, S4=5%. |
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
| Mode | `paper` — never change to `live` without explicit instruction |
| Active strategy | S1 only |
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
8. **Scenario D capital config** — S1=70%, 4 slots. S2/S3/S4 placeholders at 15%/10%/5%. Full 3-layer slot-based position sizing pending Nemawashi deep dive.
9. **Slot-based position sizing** — 3-layer calculation: risk-based shares → capital cap scale-down → viability floors (min_risk ₹1,000, min_position_value ₹15,000). Charge estimation logged per sized position. No-entry window at 14:45 IST (Gate 5b). Startup refuses if slot_capital < ₹40,000. Pending orders cancelled at hard_exit before emergency_exit_all.
10. **Futures trading gate criteria** — No futures until ALL conditions met: (a) 10 completed S1 trades (stop/target/hard_exit, not just opened), (b) 3 consecutive bug-free sessions, (c) every trade P&L verified in session report matches expected calculation, (d) at least 1 winning trade proving strategy can make money. Manual delivery trades (NIFTY BEES, large-caps) are acceptable anytime for market views — separate from TradeOS.
11. **HAWK** — AI watchlist engine. Standalone shadow-testing tool. nsepython primary + nsetools fallback. Claude Sonnet. Dual storage: JSON + TimescaleDB. Separate Telegram channel (HAWK-Picks). Development on feature/hawk branch. Full spec: `docs/hawk_spec.md`.
12. **Option C stop floor** — Minimum 2% stop distance enforced when swing-based stops are tighter. Paper capital increased ₹5L→₹10L for realistic testing. All 10 S1 strategy parameters (EMA periods, RSI thresholds, volume ratio, RR ratio, swing lookback, min stop %) now configurable via settings.yaml. Zero hardcoded numbers in signal generation.
13. **HAWK multi-model consensus** — 4 LLMs (Claude, Gemini, GPT-5.4, Kimi) run on same data. Picks scored: UNANIMOUS (4/4), STRONG (3/4), MAJORITY (2/4), SINGLE (1/4). $0.23/run, ~$10/month. All 4 models selected after side-by-side comparison — 80% pick overlap confirmed.

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
| HAWK: nsepython bhavcopy/FII-DII broken (API changed) | Using KiteConnect fallback. Delivery % unavailable. | Medium |
| HAWK: Telegram config for HAWK-Picks channel | bot_token + chat_id not yet configured in secrets.yaml | Medium |
| HAWK: Regime shows "unknown" | Needs TradeOS regime integration | Low |
| B12-B14 verify in Session 08 | Confirm correct P&L, Telegram display, exit reason in live session | High |
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
- **Production readiness** — Phased: (1) DB + token automation + log rotation + systemd, (2) Docker Compose + FastAPI + basic WebUI, (3) encrypted secrets + VPS hardening + monitoring, (4) full WebUI + HAWK UI + mobile. See session notes for detail.

---

## 8. Immediate Next Actions

1. **Session 08** — Monday March 16. Verify B12-B14 fixes (correct P&L, Telegram display, exit reason).
2. Trailing stop data gate review — March 16 (likely defer, only 2 trades, neither hit 2R).
3. DB trade history — Nemawashi design: TimescaleDB tables (trades, signals, daily_sessions). Dual-write alongside logs. Use existing TimescaleDB + asyncpg infrastructure.
4. Token semi-automation — Telegram-triggered login flow: cron sends login URL to Telegram, user taps + TOTP, callback server captures token, confirms via Telegram, starts main.py.
5. HAWK Day 3 eval Monday after Session 08: `python tools/hawk_eval.py --date 2026-03-13`
6. Futures gate: 2/10 trades, 0/3 clean sessions.

---

## 9. Session Log

| Date | Session | Summary | Outcome |
|------|---------|---------|---------|
| 2026-03-11 | Config Extraction | Capital 5L→10L, Option C stop floor 2%, all S1 params to config (`9d9f595`, `96de8fa`). Tests: 340→353. | Full playground mode — every parameter tunable from config. |
| 2026-03-11 | Session 05 + HAWK | Session 05: system health PASS, 0 trades (sizer rejected all — pre-fix). T1-T3 Telegram bugs fixed (`86c67ea`). Stop floor 2% + ₹10L capital applied (`9d9f595`). All S1 params to config (`96de8fa`). HAWK first run: 15 picks on feature/hawk. Tests: 353→361 (main). | S1 ready for Session 06. HAWK pipeline proven. |
| 2026-03-12 | Session 06 + HAWK | Config tuning: volume_ratio_min 1.5→1.2 (`26d840e`), no_entry_after 14:30→14:45 (`42fd4d5`). HAWK evaluator built (`e4fd409`). Bhavcopy embed fix (`41886c2`). Day 1 eval: 73.3% direction (11/15), HIGH 100% (4/4). Tests: 361 (main), 407 (feature/hawk). | Config tuned. HAWK eval pipeline complete. |
| 2026-03-12 | Merge + Consensus | feature/hawk merged into main (`094e04a`). 439 tests. HAWK multi-model consensus built (Claude+Gemini+GPT-5.4+Kimi). Day 2: 12 unanimous picks, $0.23/run. Model comparison completed. | Unified codebase. Session 07 ready. |
| 2026-03-13 | Session 07 + HAWK Day 3 | FIRST TRADES: SUNPHARMA SHORT +₹1,361, TITAN SHORT +₹30. Session +₹1,390 net. B12-B14 found and fixed (`af8a007`): gross P&L, Telegram fields, exit reason. `resolve_position_fields` utility. HAWK Day 2 eval: SHORT 100% (8/8). Day 3: 8 unanimous SHORT. Tests: 439→453. | Milestone — first profitable session. 2/10 trades toward futures gate. |
| 2026-03-13 | Weekend Plan | DB trade history design (TimescaleDB, dual-write) and token semi-automation (Telegram + callback server) prioritized for weekend. Production readiness roadmap brainstormed (4 phases). | New session starting for implementation. |

---

## 10. Session Rules

These rules apply to every TradeOS session regardless of context window or session reset.

1. **Nemawashi First** — No CC prompt is generated until planning is complete. Every feature goes through: problem definition → research → brainstorm → edge case mapping → cost/risk analysis → decision lock → THEN implementation. Ratio: 70-80% planning, 20-30% implementation.
2. **Living Document Protocol** — Every conclusion, decision, or significant discussion must be captured in `TradeOS_context.md` via a CC delta prompt before moving to the next topic.
3. **Context Handoff** — If a session approaches context limits, generate a handoff document and update `TradeOS_context.md` with the exact resume point before the session ends.
4. **Allocation Sum Rule** — All strategy allocations in `config/settings.yaml` MUST sum to 1.00. Validated at startup — system refuses to start if violated. Any config change to one allocation requires adjusting others to maintain sum.
5. **Position Sizing Parameters** — `min_slot_capital` (₹40,000), `min_risk_floor` (₹1,000), `min_position_value` (₹15,000) are configured in `config/settings.yaml` under `position_sizing`. `no_entry_after` (14:30 IST) is under `trading_hours`. All are startup-validated or gate-checked — never bypassed at runtime.
6. **Context Hygiene** — `TradeOS_context.md` is a rolling window, not a history book. Rules: (a) Known TODOs: Only OPEN items stay. Completed items move to `docs/context_archive.md` after 2 sessions. (b) Session Log: Keep last 5 sessions only. Older rows move to `docs/context_archive.md`. (c) Completed Work: Summarize, don't accumulate. Move details to archive when section exceeds 10 items. (d) Key Decisions and Session Rules: Stay in main file permanently (compact, always relevant). (e) Archive file is append-only — never edit or delete archived content.
7. **Telegram Channel Separation** — Each engine/module gets its own Telegram channel. Never mix notification streams. Current channels: TradeOS-Trading (S1 signals, fills, exits, heartbeat, system), HAWK-Picks (AI watchlist). New modules must define their own channel before implementation.
8. **Git Branching** — feature/* for new features, fix/* for bugs, main = production (deployed on VPS). Feature branches created from main, kept in sync with `git rebase main`. Merge to main only when fully tested and all tests pass. CC must track which branch is for which feature. Never develop new features directly on main. Current branches: main (S1 trading + HAWK AI engine, production).
9. **SHORT Position Accounting** — Negative qty for shorts. Field names: `avg_price` (not `entry_price`), `side` (not `direction`). This mismatch caused B7 (false kill switch with -₹199,679 phantom P&L). Always verify field names when accessing position data.

---

## 11. Last Updated

**2026-03-13** — Session 07 milestone (first trades +₹1,390). B12-B14 fixed. Weekend: DB design + token automation. New session starting.
