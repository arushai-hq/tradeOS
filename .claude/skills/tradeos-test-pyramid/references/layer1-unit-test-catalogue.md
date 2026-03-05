# Layer 1 — Mandatory Unit Test Catalogue

All tests listed here are non-negotiable. If any are missing from the test suite, Layer 1 is incomplete regardless of coverage percentage. These test names are the exact function names that must exist.

## tests/unit/test_tick_validator.py (11 mandatory tests)

The tick validator has 5 gates. Each gate needs both its "rejects bad input" and edge-case paths covered.

| Test name | What it verifies |
|-----------|-----------------|
| `test_gate1_rejects_zero_price` | price == 0 is discarded |
| `test_gate1_rejects_negative_price` | price < 0 is discarded |
| `test_gate2_rejects_price_above_20pct_circuit` | price > prev_close * 1.20 is discarded |
| `test_gate2_passes_when_prev_close_unavailable` | missing prev_close → gate 2 skipped (no halt) |
| `test_gate3_rejects_negative_volume` | volume < 0 is discarded |
| `test_gate3_rejects_none_volume` | volume = None is discarded |
| `test_gate4_rejects_tick_older_than_5s` | exchange_timestamp > 5s ago → discard |
| `test_gate4_uses_exchange_timestamp_not_local` | staleness measured from exchange_timestamp, not system clock |
| `test_gate5_silent_discard_duplicate` | identical tick after first → silently discarded, no exception |
| `test_valid_tick_passes_all_gates` | well-formed tick flows through all 5 gates |
| `test_validator_never_raises_exception` | malformed input (None, empty dict, missing fields) → never raises |

Key testing principle: Gate 4 uses `exchange_timestamp` from the Zerodha tick payload, not the local system clock. This is frequently wrong in naive implementations.

---

## tests/unit/test_kill_switch.py (12 mandatory tests)

Kill switch has 3 levels. The escalation semantics (Level 1 ≠ Level 2, Level 3 includes Level 2 actions) are the most error-prone area.

| Test name | What it verifies |
|-----------|-----------------|
| `test_level1_stops_new_signals` | Level 1 → new signals rejected |
| `test_level1_does_not_close_positions` | Level 1 → existing positions untouched |
| `test_level2_closes_all_positions` | Level 2 → close_all_positions() called |
| `test_level2_cancels_all_orders` | Level 2 → cancel_all_open_orders() called |
| `test_level3_halts_event_loop` | Level 3 → asyncio loop shutdown triggered |
| `test_consecutive_losses_trigger_level1` | consecutive_losses=5 AND daily_pnl_pct=-0.016 → level == 1 (compound condition both met) |
| `test_consecutive_losses_no_trigger_without_pnl_condition` | consecutive_losses=5, daily_pnl_pct=-0.005 → level == 0 (pnl condition not breached) |
| `test_consecutive_losses_no_trigger_below_threshold` | consecutive_losses=4, daily_pnl_pct=-0.020 → level == 0 (count not yet reached) |
| `test_daily_loss_3pct_triggers_level2` | daily_pnl_pct <= -0.030 → level == 2 |
| `test_kill_switch_blocks_order_placement` | is_trading_allowed() returns False at any level >= 1 |
| `test_no_auto_reset_during_market_hours` | reset() raises during 09:15–15:30 IST window |
| `test_level3_executes_level2_actions_first` | Level 3 trigger → Level 2 actions verified before loop halt |

---

## tests/unit/test_order_state_machine.py (10 mandatory tests)

The state machine has 8 states and defined valid transitions. Invalid transitions must raise, not silently succeed.

| Test name | What it verifies |
|-----------|-----------------|
| `test_happy_path_created_to_filled` | CREATED→SUBMITTED→ACKNOWLEDGED→FILLED succeeds |
| `test_invalid_transition_raises_exception` | CREATED→FILLED direct transition raises |
| `test_created_to_filled_direct_is_invalid` | explicit test: CREATED→FILLED is not a valid arc |
| `test_duplicate_order_same_symbol_rejected` | placing same symbol twice while first is open → rejected |
| `test_partial_fill_not_treated_as_complete` | PARTIALLY_FILLED order must not trigger position-open logic |
| `test_rejected_increments_consecutive_counter` | REJECTED state → consecutive_loss_counter += 1 |
| `test_zerodha_status_open_maps_to_acknowledged` | Zerodha "OPEN" string → ACKNOWLEDGED state |
| `test_zerodha_status_complete_maps_to_filled` | Zerodha "COMPLETE" string → FILLED state |
| `test_unknown_zerodha_status_maps_to_unknown` | unrecognized status string → UNKNOWN state, no exception |
| `test_startup_reconciliation_blocks_on_unknown` | order in UNKNOWN state at startup → instrument locked |

---

## tests/unit/test_risk_manager.py (6 mandatory tests)

Risk parameters are hardcoded constants (from config/settings.yaml). Tests must use those exact values — no magic numbers.

| Test name | What it verifies |
|-----------|-----------------|
| `test_position_size_respects_1pt5pct_limit` | calculated qty never risks > 1.5% of S1 capital |
| `test_max_3_positions_blocks_new_entry` | shared_state["open_positions"] == 3 → new entry rejected |
| `test_hard_exit_time_1500_ist_triggers` | clock at 15:00 IST → all positions closed, no new entries |
| `test_stop_loss_mandatory_on_every_order` | order without stop_loss → rejected |
| `test_daily_loss_accumulates_correctly` | three trades with known P&L → daily_pnl_pct sum correct |
| `test_consecutive_loss_counter_resets_on_win` | 2 losses + 1 win → counter back to 0 |

---

## tests/unit/test_s1_strategy.py (10 mandatory tests)

S1 entry conditions have 4 required factors for long, 4 for short. A signal must only fire when ALL 4 are met.

| Test name | What it verifies |
|-----------|-----------------|
| `test_long_signal_requires_ema9_above_ema21` | ema9 < ema21 → no long signal |
| `test_long_signal_requires_price_above_vwap` | price < vwap → no long signal |
| `test_long_signal_requires_rsi_between_55_and_70` | rsi outside [55, 70] → no long signal |
| `test_long_signal_requires_volume_1pt5x_average` | volume < 1.5x avg → no long signal |
| `test_short_signal_requires_ema9_below_ema21` | ema9 > ema21 → no short signal |
| `test_short_signal_requires_price_below_vwap` | price > vwap → no short signal |
| `test_no_signal_when_conditions_partially_met` | 3 of 4 conditions met → no signal |
| `test_signal_respects_kill_switch_gate` | kill switch active → no signal generated |
| `test_1_to_2_rr_target_calculated_correctly` | target = entry + 2 * (entry - stop_loss) for long |
| `test_stop_loss_at_previous_swing_low` | stop_loss assigned from previous_swing_low field |

---

## Coverage Requirement

Run with: `pytest --cov=risk_manager --cov=data_engine --cov=strategies/s1_intraday --cov-report=term-missing`

Target: **> 90%** on all three modules. If coverage is below 90% on any module, Layer 1 is not passing even if all named tests are present. The coverage gap indicates untested branches — likely error paths or edge cases.

## Speed Requirement

Full Layer 1 suite must complete in **< 60 seconds**. If it takes longer, the test suite will be skipped during development, which defeats the purpose. Slow tests are usually caused by: real I/O calls (network, disk), `time.sleep()` calls, or un-mocked external dependencies. Use `freezegun` for time, mock all Zerodha API calls.
