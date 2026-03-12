# TradeOS — Context Archive

Historical record of completed work, resolved bugs, and past session logs.
This file is append-only. Current state lives in TradeOS_context.md (repo root).

---

## Archived TODOs (Resolved)

<!-- Completed TODOs move here after 2 sessions -->

| Bug | Impact | Priority |
|-----|--------|----------|
| ✅ B1 `hard_exit_triggered` at 15:00 does not close open positions — fixed: `emergency_exit_all` via `risk_watchdog` (commit `9ca7502`) | CRITICAL — resolved | Fixed |
| ✅ B2 No time gate preventing signal generation after hard_exit — fixed: `accepting_signals` halt gate in `strategy_engine._process_tick` (commit `9ca7502`) | CRITICAL — resolved | Fixed |
| ✅ B3 SHORT signals generated on oversold RSI (~30) — fixed: f65f8af — SHORT RSI filter was checking 30≤rsi≤45 instead of rsi≥45. Oversold shorts now rejected. | HIGH — resolved | Fixed |
| ✅ B4 `daily_pnl_pct` stuck at 0.0 — fixed: f0a1cf1 — shared_state `last_tick_prices` populated from validated ticks; heartbeat computes realized+unrealized P&L every 30s | HIGH — resolved | Fixed |
| ✅ B5 Paper mode missing lifecycle logging — fixed: ca7ddc9 — 7 lifecycle events added: signal_accepted, signal_rejected, order_placed, order_filled, stop_hit, target_hit, position_closed | HIGH — resolved | Fixed |
| ✅ B6 `Queue.put_nowait` overflow exceptions at ~15:44 — fixed: be16168 — `_safe_enqueue()` wraps `put_nowait` with `QueueFull` catch; overflow warning logged once, further drops suppressed | MEDIUM — resolved | Fixed |
| ✅ B7 Unrealized P&L formula broken for SHORT positions — fixed: `cc9c018`. Field name mismatch (`entry_price`→`avg_price`, `direction`→`side`, qty sign). Added no-tick guard. Phantom -₹199,679 loss → false kill switch in Session 04. | CRITICAL — resolved | Fixed |
| ✅ B8 Exit fill handler creates ghost LONG positions — fixed: `7ed6b7a`. `_on_exit_fill` snapshots position data before `on_close` deletes it. Removed duplicate `position_closed` log from OrderMonitor (PnlTracker is authoritative source). | CRITICAL — resolved | Fixed |
| ✅ B9 Session report parser shows duplicate signals/trades — fixed: `028995d`. Parser deduplicates signals/trades within 5s window, filters ghost entries (entry_price=0, qty=0). | MEDIUM — resolved | Fixed |
| ✅ B10 94 pre-market warnings before 09:15 — fixed: `028995d`. `nifty_intraday_unavailable`, `vix_data_unavailable`, `prev_close_load_failed`, `heartbeat_no_ticks_30s` downgraded to DEBUG before 9:15 via `is_market_hours()` gate. | LOW — resolved | Fixed |
| ✅ B11 Regime detector double-initializes at startup — fixed: `028995d`. `_initialized` guard prevents double-init. Removed duplicate `regime_initialized` log from main.py. | LOW — resolved | Fixed |

---

## Archived Session Log

<!-- Session log rows move here when TradeOS_context.md exceeds 5 sessions -->

| Date | Session | Summary | Outcome |
|------|---------|---------|---------|
| 2026-03-06 | Session 01 | 6hr, 129k ticks, 340 candles, 0 signals | VWAP field bug found and fixed |
| 2026-03-07 | Session 02 | Signal pipeline validated post VWAP fix | Regime gating confirmed active |
| 2026-03-09 | Session 03 | 4h 44m, 9 signals (5L/4S), bear_trend → high_vol at 15:05 | Debrief pending |
| 2026-03-09 | — | New Claude session created (context limit). Living document established. | `TradeOS_context.md` created |
| 2026-03-09 | Session 03 Debrief | 9 signals, 3 positions, 6 bugs found (B1–B6). First session with live trades. | Debrief complete, fix list generated |
| 2026-03-09 | Bug Fixes B1–B3+B5 | Fixed hard exit (B1), signal halt gate (B2), RSI filter inversion (B3), lifecycle logging (B5). Tests: 222→249. | 4 of 6 bugs resolved. Ready for Session 04. |
| 2026-03-09 | Bug Fixes B4+B6 | Fixed PnL tracker (B4: real-time unrealized P&L in heartbeat), queue overflow (B6: safe enqueue with overflow suppression). All 6 Session 03 bugs resolved. Tests: 249→260. | Session 04 ready. |
| 2026-03-09 | Test Fix | Fixed 2 time-dependent test failures caused by B1/B2 hard_exit gate. Tests: 260→262, 0 failures. | Clean test suite for Session 04. |
| 2026-03-09 | Tooling | Rich Telegram alerts (`cdd066b`) + session report CLI (`4559b7a`). Tests: 262→299. Session 03 re-analysis revealed all accepted trades were oversold SHORTs. | Visibility tooling complete. |

---

## Archived Completed Work

<!-- Historical completed work items move here periodically -->
