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

**Strategy:** S1 Intraday Momentum — EMA9/21 crossover + VWAP + RSI 55–70 (LONG) / 30–45 (SHORT) + volume ratio ≥ 1.5x
**Watchlist:** 20 hardcoded NSE stocks in `config/settings.yaml`
**Candle interval:** 15 minutes | **Trade window:** 09:15–15:00 IST | **Hard exit:** 15:00 IST

---

## 3. Current State

| Item | Status |
|------|--------|
| Tests | **211 passing**, 12 skipped (DB_DSN) — commit `89f1fde` |
| Mode | `paper` — never change to `live` without explicit instruction |
| Active strategy | S1 only |
| Paper Session 01 | Complete — VWAP bug found and fixed |
| Paper Session 02 | Complete — signal pipeline validated |
| Paper Session 03 | Complete — debrief complete. 9 signals generated, 3 converted to positions. 6 bugs found (2 critical, 3 high, 1 medium). Zero P&L tracked — tracker bug. First session with live signal generation and position entry. |

---

## 4. Key Decisions

1. **Depth before breadth** — Validate S1 fully before building S2.
2. **Data-gated trailing stop** — Design spec complete (`docs/strategy_specs/trailing_stop_spec.md`). Blocked until ≥ 5 trades past 2R target. First review: **2026-03-16**.
3. **Dashboard deferred** — Build after 3–4 sessions with real P&L data. No premature UI.
4. **Cosmetic DB bugs tolerated** — `tradingsymbol`, `bid`, `ask` fields null in tick storage. Zero impact on signal generation or risk logic.
5. **AI/LLM dynamic watchlist parked** — Revisit after S1 validated on fixed watchlist.

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

---

## 6. Known TODOs

| Bug | Impact | Priority |
|-----|--------|----------|
| B1 `hard_exit_triggered` at 15:00 does not close open positions — 3 positions orphaned until 15:30 shutdown | CRITICAL — open positions unmanaged for 30 min | Critical |
| B2 No time gate preventing signal generation after hard_exit — 3 signals generated at/after 15:00 | CRITICAL — signals should be suppressed post hard_exit | Critical |
| B3 SHORT signals generated on oversold RSI (~30) — RSI filter likely inverted for SHORT direction | HIGH — incorrect SHORT entries | High |
| B4 `daily_pnl_pct` stuck at 0.0 despite open positions — PnL tracker not computing unrealized P&L | HIGH — no live P&L visibility | High |
| B5 Paper mode missing lifecycle logging — no `order_filled`, `position_closed`, `stop_hit`, `target_hit` events | HIGH — debrief blind without fill events | High |
| B6 `Queue.put_nowait` overflow exceptions at ~15:44 — tick queue full after market close | MEDIUM — post-close only, zero trading impact | Medium |
| `tradingsymbol` null in `ticks` table | Cosmetic — token present, symbol lookup works | Low |
| `bid` / `ask` null in `ticks` table | Cosmetic — not used in S1 logic | Low |

---

## 7. Deferred Roadmap

- **AI/LLM dynamic watchlist** — Screen NSE universe daily; select top momentum candidates
- **S2 multi-regime short strategy** — BEAR_TREND / HIGH_VOLATILITY regime-aware entries
- **Admin dashboard** — Mobile/iPad SaaS; session P&L, signal log, regime status. Build after 3–4 sessions.

---

## 8. Immediate Next Actions

1. Fix B1 (`hard_exit` position closure) and B2 (post-exit signal gate) — CRITICAL, before next session
2. Fix B3 (RSI filter inversion for shorts) — before next session
3. Add B5 lifecycle logging — before next session (needed for future debriefs)
4. Fix B4 (PnL tracker) and B6 (queue overflow) — can be Session 04 or 05
5. Run Session 04 paper trading with fixes applied
6. Review trailing stop data gate on **2026-03-16**

---

## 9. Session Log

| Date | Session | Summary | Outcome |
|------|---------|---------|---------|
| 2026-03-06 | Session 01 | 6hr, 129k ticks, 340 candles, 0 signals | VWAP field bug found and fixed |
| 2026-03-07 | Session 02 | Signal pipeline validated post VWAP fix | Regime gating confirmed active |
| 2026-03-09 | Session 03 | 4h 44m, 9 signals (5L/4S), bear_trend → high_vol at 15:05 | Debrief pending |
| 2026-03-09 | — | New Claude session created (context limit). Living document established. | `TradeOS_context.md` created |
| 2026-03-09 | Session 03 Debrief | 9 signals, 3 positions, 6 bugs found (B1–B6). First session with live trades. | Debrief complete, fix list generated |

---

## 10. Last Updated

**2026-03-09** — Session 03 debrief complete. 6 bugs catalogued (B1–B6). Fix priority: B1→B2→B3→B5→B4→B6.
