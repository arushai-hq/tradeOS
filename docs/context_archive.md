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
| 2026-03-10 | Config Fix | S1 allocation 30%→70%, max positions 3→4, allocation sum validation added (`692e9f8`). Tests: 299→303. | Session 04 ready with Scenario D config. |
| 2026-03-10 | Position Sizing | Slot-based 3-layer sizing (`361876e`), no-entry window 14:30 IST (`c60648f`), min slot capital + pending order cancel (`c862313`). Tests: 303→318. | Nemawashi complete. |
| 2026-03-10 | Session 04 Debrief | 2 trades (LT SHORT, AXISBANK SHORT). Kill switch false-triggered at 30s — phantom unrealized P&L -₹199,679 from B7. Ghost positions from B8. Net P&L: -₹239 (charges). Slot-based sizing worked correctly. | 2 critical bugs (B7, B8), 3 minor (B9-B11). |
| 2026-03-10 | Bug Fixes B7+B8 | B7: unrealized P&L field mismatch fixed (`cc9c018`). B8: exit fill snapshot-before-delete (`7ed6b7a`). Tests: 318→329. | Session 05 ready. Two critical Session 04 bugs resolved. |
| 2026-03-10 | Bug Fixes B9-B11 | B9: report parser hardened (`028995d`). B10: pre-market warnings gated (`028995d`). B11: regime double-init fixed (`028995d`). All Session 04 bugs resolved. Tests: 329→340. | Session 05 ready. Clean system. |
| 2026-03-11 | HAWK Design + Rules | HAWK spec complete (`docs/hawk_spec.md`). Telegram channel separation rule added. Git branching model established (feature/*/fix/*/main). | Design + engineering practices locked. |

---

## Archived TODOs (Resolved) — B12-B14

| Bug | Impact | Priority |
|-----|--------|----------|
| ✅ B12 gross_pnl=0.0 on position close — fixed: `af8a007`. emergency_exit_all used entry_price as exit_price; now uses tick price. | CRITICAL — resolved | Fixed |
| ✅ B13 Telegram heartbeat wrong entry/direction — fixed: `af8a007`. Telegram read entry_price/direction from shared_state which uses avg_price/side. Now uses resolve_position_fields(). | HIGH — resolved | Fixed |
| ✅ B14 exit_reason=KILL_SWITCH instead of HARD_EXIT_1500 — fixed: `af8a007`. emergency_exit_all now accepts exit_type parameter; hard exit passes "HARD_EXIT". | MEDIUM — resolved | Fixed |

---

## Archived Completed Work

<!-- Historical completed work items move here periodically -->
