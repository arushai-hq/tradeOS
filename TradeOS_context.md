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

**Strategy:** S1 Intraday Momentum — EMA9/21 crossover + VWAP + RSI 55–70 (LONG) / 30–45 (SHORT) + volume ratio ≥ 1.5x
**Watchlist:** 20 hardcoded NSE stocks in `config/settings.yaml`
**Candle interval:** 15 minutes | **Trade window:** 09:15–15:00 IST | **Hard exit:** 15:00 IST

---

## 3. Current State

| Item | Status |
|------|--------|
| Tests | **340 passing, 0 failures, 12 skipped** — commit `028995d` |
| S1 allocation | 70% (₹3,50,000). Max positions: 4. S2=15%, S3=10%, S4=5%. |
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
9. **Slot-based position sizing** — 3-layer calculation: risk-based shares → capital cap scale-down → viability floors (min_risk ₹1,000, min_position_value ₹15,000). Charge estimation logged per sized position. No-entry window at 14:30 IST (Gate 5b). Startup refuses if slot_capital < ₹40,000. Pending orders cancelled at hard_exit before emergency_exit_all.
10. **Futures trading gate criteria** — No futures until ALL conditions met: (a) 10 completed S1 trades (stop/target/hard_exit, not just opened), (b) 3 consecutive bug-free sessions, (c) every trade P&L verified in session report matches expected calculation, (d) at least 1 winning trade proving strategy can make money. Manual delivery trades (NIFTY BEES, large-caps) are acceptable anytime for market views — separate from TradeOS.
11. **HAWK** — AI watchlist engine. Standalone shadow-testing tool. nsepython primary + nsetools fallback. Claude Sonnet. Dual storage: JSON + TimescaleDB. Separate Telegram channel (HAWK-Picks). Development on feature/hawk branch. Full spec: `docs/hawk_spec.md`.

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

---

## 7. Deferred Roadmap

- **HAWK (AI Market Intelligence Engine)** — Design spec complete at `docs/hawk_spec.md`. Branch: feature/hawk. Standalone daily tool: evening + morning runs, nsepython + nsetools + KiteConnect data, Claude Sonnet LLM, JSON + DB + Telegram (HAWK-Picks channel). Shadow-testing alongside S1. Implementation: 2-3 CC prompts when ready.
- **S2 multi-regime short strategy** — BEAR_TREND / HIGH_VOLATILITY regime-aware entries
- **Admin dashboard** — Mobile/iPad SaaS; session P&L, signal log, regime status. Build after 3–4 sessions.
- **Futures paper trading** — NIFTY futures alongside S1. Gated on: 10 completed S1 trades + 3 clean sessions + verified P&L + 1 winner. Infrastructure needed: lot-aware position sizer, expiry management, margin monitoring. Design (Nemawashi) can begin during S1 validation phase — no code until gates clear.

---

## 8. Immediate Next Actions

1. **Run Session 05** paper trading — all Session 04 bugs resolved, clean system
2. **Verify:** (a) no false kill switch, (b) no ghost positions, (c) positions hold until stop/target/hard_exit, (d) report shows correct counts, (e) clean pre-market logs, (f) single regime init
3. Continue Nemawashi capital management monitoring (slot-based sizing live, observing)
4. Review trailing stop data gate on **2026-03-16**

---

## 9. Session Log

| Date | Session | Summary | Outcome |
|------|---------|---------|---------|
| 2026-03-09 | Test Fix | Fixed 2 time-dependent test failures caused by B1/B2 hard_exit gate. Tests: 260→262, 0 failures. | Clean test suite for Session 04. |
| 2026-03-09 | Tooling | Rich Telegram alerts (`cdd066b`) + session report CLI (`4559b7a`). Tests: 262→299. Session 03 re-analysis revealed all accepted trades were oversold SHORTs. | Visibility tooling complete. |
| 2026-03-10 | Config Fix | S1 allocation 30%→70%, max positions 3→4, allocation sum validation added (`692e9f8`). Tests: 299→303. | Session 04 ready with Scenario D config. |
| 2026-03-10 | Position Sizing | Slot-based 3-layer sizing (`361876e`), no-entry window 14:30 IST (`c60648f`), min slot capital + pending order cancel (`c862313`). Tests: 303→318. | Nemawashi complete. |
| 2026-03-10 | Session 04 Debrief | 2 trades (LT SHORT, AXISBANK SHORT). Kill switch false-triggered at 30s — phantom unrealized P&L -₹199,679 from B7. Ghost positions from B8. Net P&L: -₹239 (charges). Slot-based sizing worked correctly. | 2 critical bugs (B7, B8), 3 minor (B9-B11). |
| 2026-03-10 | Bug Fixes B7+B8 | B7: unrealized P&L field mismatch fixed (`cc9c018`). B8: exit fill snapshot-before-delete (`7ed6b7a`). Tests: 318→329. | Session 05 ready. Two critical Session 04 bugs resolved. |
| 2026-03-10 | Bug Fixes B9-B11 | B9: report parser hardened (`028995d`). B10: pre-market warnings gated (`028995d`). B11: regime double-init fixed (`028995d`). All Session 04 bugs resolved. Tests: 329→340. | Session 05 ready. Clean system. |
| 2026-03-11 | HAWK Design + Rules | HAWK spec complete (`docs/hawk_spec.md`). Telegram channel separation rule added. Git branching model established (feature/*/fix/*/main). | Design + engineering practices locked. |

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
8. **Git Branching** — feature/* for new features, fix/* for bugs, main = production (deployed on VPS). Feature branches created from main, kept in sync with `git rebase main`. Merge to main only when fully tested and all tests pass. CC must track which branch is for which feature. Never develop new features directly on main. Current branches: main (S1 trading engine, production).

---

## 11. Last Updated

**2026-03-11** — HAWK design spec created. Telegram separation and git branching rules established as permanent session rules.
