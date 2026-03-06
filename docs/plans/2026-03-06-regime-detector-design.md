# Regime Detector — Design Document

> Approved: 2026-03-06
> Author: Irfan + Claude Code
> Status: Ready for implementation

---

## Problem

S1 Intraday Momentum generates long and short signals without awareness of
the broader market environment. A long signal during a market crash or a
short signal during a strong bull trend wastes capital and increases drawdown.

The regime detector classifies the market into one of four regimes and gates
S1 signals accordingly — blocking counter-trend entries and reducing position
size during volatile conditions.

---

## Four Regimes

Evaluated in strict priority order (first match wins):

| Priority | Regime | Trigger | Longs | Shorts | Size Multiplier |
|----------|--------|---------|-------|--------|-----------------|
| 1 | CRASH | VIX > 35 OR Nifty intraday drop > 2.5% | BLOCKED | Allowed if volume_ratio > 2.0 | 0.5 |
| 2 | HIGH_VOLATILITY | VIX 25–35 OR Nifty intraday range > 1.5% | Allowed | Allowed | 0.5 |
| 3 | BEAR_TREND | Nifty price < 200 EMA AND VIX >= 15 | BLOCKED | Allowed | 1.0 |
| 4 | BULL_TREND | Default (none of above) | Allowed | BLOCKED | 1.0 |

### Key decision: CRASH multiplier = 0.5 (not 0.0)

Original spec had 0.0 which contradicts allowing shorts in CRASH.
Resolved: 0.5 — shorts with extra volume confirmation execute at half size.
Longs blocked entirely by is_long_allowed() returning False.

---

## Data Sources

All via `kite.historical_data()` (REST). No WebSocket subscription for indices.

```
NIFTY_50_TOKEN  = 256265   # NSE:NIFTY 50
INDIA_VIX_TOKEN = 264969   # NSE:INDIA VIX
```

| Data | Endpoint | When |
|------|----------|------|
| Nifty 200-day EMA | historical_data(256265, 200 days, "day") | Session start only |
| Nifty intraday OHLC | historical_data(256265, today, "minute") | Every 60s refresh |
| India VIX | historical_data(264969, today, "day") | Every 60s refresh |

60s refresh latency is acceptable — regime is a macro filter, not tick-level.
VIX does not spike from 20 to 35 in under a minute.

---

## Architecture

```
Phase 1 Startup (main.py):
  DB pool → RegimeDetector.initialize() → DataEngine → RiskManager
    → StrategyEngine(regime_detector) → ExecutionEngine → system_ready

Phase 2 Runtime:
  risk_watchdog (every 60s) → regime_detector.refresh()
    → re-fetch Nifty + VIX via REST
    → re-classify → update shared_state
    → if changed: log WARNING + Telegram

Signal Pipeline:
  SignalGenerator.evaluate() → Signal
    → RiskGate.check() [Gates 0–6 unchanged]
      → Gate 7: regime gate
        → is_long_allowed() / is_short_allowed()
        → block reason: REGIME_BLOCKED_BULL / REGIME_BLOCKED_BEAR / etc.
    → order_queue
```

---

## Class Interface

```python
class RegimeDetector:
    def __init__(self, kite: KiteConnect, config: dict): ...

    async def initialize(self) -> MarketRegime:
        # Phase 1: fetch 200-day Nifty, VIX, intraday → classify + cache

    async def refresh(self) -> MarketRegime:
        # Phase 2: re-fetch intraday + VIX → re-classify
        # On change: log WARNING + Telegram
        # On failure: keep last regime, 3-strike Telegram alert

    def current_regime(self) -> MarketRegime:
        # Synchronous cache read — safe from signal_processor

    def is_long_allowed(self) -> bool:
    def is_short_allowed(self) -> bool:
    def position_size_multiplier(self) -> float:
        # 1.0 normal, 0.5 in HIGH_VOLATILITY and CRASH
```

---

## Integration Touchpoints

### 1. main.py
- `_init_shared_state()`: add `"market_regime": None`, `"regime_position_multiplier": 1.0`
- Phase 1: create RegimeDetector after DB pool, before DataEngine
- Pass regime_detector to StrategyEngine constructor
- risk_watchdog: call `regime_detector.refresh()` every 60s

### 2. strategy_engine/__init__.py
- Accept `regime_detector` param in constructor
- Pass to RiskGate constructor

### 3. strategy_engine/risk_gate.py
- Accept `regime_detector` in RiskGate constructor
- Add Gate 7 after Gate 6: regime check
- CRASH + SHORT: extra volume_ratio > 2.0 check on the signal

### 4. shared_state
- `"market_regime"`: MarketRegime.value string, updated on every classify
- `"regime_position_multiplier"`: float, updated on every classify

---

## Resilience (D3)

- API failure in refresh(): log WARNING, keep last known regime
- 3 consecutive failures: Telegram "regime detector degraded — using stale regime"
- Counter resets on successful refresh
- Invalid data (nifty_price <= 0, VIX > 100): log ERROR, keep last regime

---

## Files

### Created (new)
```
regime_detector/__init__.py
regime_detector/regime_detector.py
regime_detector/indicators.py
regime_detector/README.md
tests/unit/test_regime_detector.py
docs/strategy_specs/regime_detector_spec.md
```

### Modified (existing)
```
main.py                          — Phase 1 init + risk_watchdog 60s refresh
strategy_engine/__init__.py      — accept regime_detector param
strategy_engine/risk_gate.py     — Gate 7 regime check
```

---

## Test Plan (22 tests)

### Classification (8 tests)
- bull_trend: nifty > ema, vix=12
- bear_trend: nifty < ema, vix=18
- high_vol from vix: vix=28
- high_vol from range: range=1.8%
- crash from vix: vix=38
- crash from drop: drop=3.0%
- crash priority over bear: vix=40, nifty < ema
- high_vol priority over bear: vix=28, nifty < ema

### Signal gates (11 tests)
- long allowed/blocked per regime (4)
- short allowed/blocked per regime (4)
- position multiplier: normal=1.0, high_vol=0.5, crash=0.5 (3)

### Resilience (3 tests)
- regime unchanged on API failure
- telegram after 3 consecutive failures
- regime change triggers telegram

### Validation (3 tests — bonus, covers edge cases from spec)
- invalid nifty price keeps last regime
- insufficient history warns but continues
- invalid vix keeps last regime

---

*TradeOS — Arushai Systems Private Limited*
