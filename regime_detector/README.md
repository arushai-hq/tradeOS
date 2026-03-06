# Regime Detector

Real-time market regime classifier for TradeOS. Runs as part of the risk pipeline and gates strategy signals based on current market conditions.

---

## Purpose

Prevents the S1 intraday momentum strategy from operating in conditions where its edge does not hold. The detector classifies the market into one of four regimes and updates the RiskGate accordingly.

---

## Regimes

Evaluated in priority order. The first matching condition wins.

| Priority | Regime | Condition |
|----------|--------|-----------|
| 1 | `CRASH` | India VIX > 35 **OR** intraday drop > 2.5% from previous close |
| 2 | `HIGH_VOLATILITY` | India VIX 25–35 **OR** intraday range > 1.5% |
| 3 | `BEAR_TREND` | Nifty 50 < 200-day EMA **AND** VIX >= 15 |
| 4 | `BULL_TREND` | Default — none of the above conditions met |

---

## Signal Gate Rules

| Regime | Allowed Directions | Position Size Multiplier | Extra Conditions |
|--------|-------------------|--------------------------|-----------------|
| `BULL_TREND` | Longs only | 1.0 | None |
| `BEAR_TREND` | Shorts only | 1.0 | None |
| `HIGH_VOLATILITY` | Longs and shorts | 0.5 | None |
| `CRASH` | Shorts only | 0.5 | `volume_ratio > 2.0` required |

Gate logic lives in `risk_manager/risk_gate.py` as Gate 7.

---

## Data Sources

| Instrument | Kite Token | Fetch Method |
|------------|------------|--------------|
| Nifty 50 | `256265` | `kite.historical_data()` REST API |
| India VIX | `264969` | `kite.historical_data()` REST API |

Historical data is fetched for the current session day. The 200-day EMA is computed from the last 200 daily candles of Nifty 50.

---

## Integration Points

- **RiskGate Gate 7** (`risk_manager/risk_gate.py`) — calls `RegimeDetector.current_regime()` on every signal evaluation.
- **risk_watchdog** — refreshes regime every 60 seconds via `RegimeDetector.refresh()`.
- **StrategyEngine constructor** — instantiates `RegimeDetector` at startup and passes it to RiskGate.

---

## Failure Modes

| Failure | Behaviour |
|---------|-----------|
| Kite API unavailable on refresh | Regime stays at last known value (stale). No new signals blocked solely on this. |
| 3 consecutive API failures | Telegram alert sent: `"regime_detector: 3 consecutive API failures, regime is stale"` |
| Regime changes | Telegram alert sent with old and new regime values |

---

## Alerts

All alerts use the Telegram notifier from `risk_manager/observability.py`.

```python
# Regime change alert
"regime_change: BULL_TREND → BEAR_TREND"

# Stale regime alert (3-strike)
"regime_detector: 3 consecutive API failures, regime is stale"
```

---

## Running Tests

```bash
python -m pytest tests/unit/test_regime_detector.py -v
```

---

## Files

```
regime_detector/
    __init__.py          — exports RegimeDetector, Regime
    regime_detector.py   — classification logic, refresh loop
```
