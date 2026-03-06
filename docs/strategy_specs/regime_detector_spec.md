# Regime Detector — Strategy Specification

**Module:** `regime_detector/regime_detector.py`
**Phase:** 1 (active)
**Last updated:** Session 8

---

## Design Rationale

S1 intraday momentum relies on trend continuation and predictable price discovery. That edge degrades or reverses in three conditions:

1. **Crash** — panic selling creates gap-and-reverse patterns; longs get destroyed.
2. **High volatility** — whipsaws generate false breakouts; position sizing must shrink.
3. **Bear trend** — upward momentum signals fail at a higher rate; only short setups retain edge.

Without regime awareness, S1 would issue long signals during bear rallies and crash bounces, eroding the strategy's statistical edge and breaching daily loss limits more frequently.

---

## Classification Algorithm

Regimes are evaluated in strict priority order. The first matching condition is applied.

```
IF India VIX > 35 OR intraday_drop_pct > 2.5%:
    regime = CRASH                  # Priority 1

ELSE IF India VIX in [25, 35] OR intraday_range_pct > 1.5%:
    regime = HIGH_VOLATILITY        # Priority 2

ELSE IF nifty_close < nifty_ema_200 AND india_vix >= 15:
    regime = BEAR_TREND             # Priority 3

ELSE:
    regime = BULL_TREND             # Priority 4 (default)
```

The intraday metrics (`intraday_drop_pct`, `intraday_range_pct`) are computed from the current day's Nifty 50 candles relative to the previous session close.

---

## Threshold Justification

Thresholds are derived from India VIX historical behaviour on NSE:

| Threshold | Rationale |
|-----------|-----------|
| VIX > 35 | Corresponds to crisis-level events (COVID crash, 2008). Nifty moves > 3–5% intraday are common. |
| VIX 25–35 | Elevated uncertainty. Intraday ranges expand; stop losses hit more frequently. |
| VIX 15 (bear floor) | India VIX rarely sustains below 10–12. VIX >= 15 in a downtrend confirms structural risk-off. |
| Intraday drop > 2.5% | Empirically, drops of this magnitude in a single session coincide with forced liquidations and circuit breaker risk. |
| Intraday range > 1.5% | A 1.5% H-L range on Nifty intraday indicates expanded ATR; momentum strategies face adverse fills. |
| 200-day EMA | Standard institutional trend filter. Price below 200 EMA = medium-term downtrend consensus. |

---

## Signal Gate Matrix

| Regime | Long Signals | Short Signals | Size Multiplier | Extra Gate |
|--------|-------------|---------------|-----------------|------------|
| `BULL_TREND` | Allowed | Blocked | 1.0 | None |
| `BEAR_TREND` | Blocked | Allowed | 1.0 | None |
| `HIGH_VOLATILITY` | Allowed | Allowed | 0.5 | None |
| `CRASH` | Blocked | Allowed | 0.5 | `volume_ratio > 2.0` |

Gate 7 in `risk_manager/risk_gate.py` enforces this matrix. A blocked signal is discarded with a structured log entry and does not trigger a kill switch.

---

## Position Sizing Impact

The multiplier is applied to the base lot size computed by the position sizer before order placement:

```python
final_qty = base_qty * regime.size_multiplier
```

In `HIGH_VOLATILITY` and `CRASH`, this halves exposure. This is additive with any other multiplier applied by the kill switch or drawdown tracker — the minimum multiplier wins.

---

## Refresh Cadence

The regime is refreshed every 60 seconds by the `risk_watchdog` background task. During the pre-market window, the regime is initialised once before the first signal cycle begins.

Staleness does not cause a hard block on signals. A Telegram alert is sent after 3 consecutive failed refreshes so the operator can intervene.

---

## Future Extensions

| Phase | Strategy | Regime Integration |
|-------|----------|--------------------|
| Phase 2 | S2 swing | Regime drives holding period limits; CRASH exits all overnight positions |
| Phase 3 | S3 options | Regime selects IV-appropriate strike distance; CRASH activates protective puts logic |
| Phase 4 | S4 macro | Regime is an input feature to the ML signal model |

The `Regime` enum and `RegimeDetector` interface are designed to be extended without breaking existing gate logic.

---

## Backtesting Validation Note

Before Phase 2 deployment, regime classifications should be validated against historical Nifty 50 + India VIX daily data (minimum 5 years). Validation criteria:

- CRASH regime should capture all NSE circuit breaker days.
- BEAR_TREND regime should align with sustained drawdown periods (>10% from peak).
- BULL_TREND misclassification rate (labelled bull but market fell >1% next session) should be below 30%.

Use `backtester/` harness with a replay of historical ticks and VIX data to measure S1 PnL degradation per regime.
