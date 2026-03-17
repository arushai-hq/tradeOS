# TradeOS Strategy Specification: S1v2 + S1v3

**Version:** 1.0.0
**Date:** 2026-03-17
**Session:** TradeOS-03
**Status:** Locked — ready for implementation

---

## 1. Context

S1 (EMA9/21 crossover + RSI + VWAP + volume) is proven unprofitable across ALL parameter combinations (198 days backtested, best result: -₹1,16,184). Root cause: EMA crossover enters too late — momentum is exhausted by the time the signal fires. 31% win rate in a pure bull market confirms the entry logic is structurally flawed.

Two replacement strategies designed from research into 7 proven short-term traders (Kotegawa, Paul Tudor Jones, Raschke, Schwartz, Minervini, Cameron, Seykota). Common principles extracted:

1. Enter on pullback or reversal, never on exhausted momentum
2. Asymmetric risk:reward (minimum 3:1)
3. Regime awareness — don't trade choppy markets
4. Fast exits on wrong trades (price stop + time stop)
5. Directional filter — never trade against the prevailing trend

---

## 2. Strategy S1v2 — Trend Pullback

**Archetype:** Raschke "Holy Grail" + Schwartz 10-EMA filter + PTJ risk management
**Edge:** Buy the first pullback in a confirmed trend. Enter early (at the pullback), not late (at the crossover).

### 2.1 Timeframes

- **Trend assessment:** 15-minute chart (directional filter + ADX)
- **Entry execution:** 5-minute chart (pullback detection + trigger)

### 2.2 Signal Flow

STEP 1 — Directional Filter (15min)
  IF price > 10-EMA(15min) → LONG bias only
  IF price < 10-EMA(15min) → SHORT bias only

STEP 2 — Trend Confirmation (15min)
  IF ADX(14, 15min) > 25 → Trend confirmed, proceed
  IF ADX(14, 15min) <= 25 → NO TRADE (ranging market)

STEP 3 — Pullback Detection (5min)
  FOR LONG: Price closes below 20-EMA(5min) — pullback in progress
  FOR SHORT: Price closes above 20-EMA(5min) — pullback in progress
  Track: is this the FIRST pullback since ADX(14,15min) last crossed above 25?
  IF not the first pullback → SKIP (reduced win probability)

STEP 4 — Entry Trigger (5min)
  FOR LONG: After pullback, first candle that CLOSES ABOVE 20-EMA(5min)
  FOR SHORT: After pullback, first candle that CLOSES BELOW 20-EMA(5min)
  This is the "reclaim" — price pulled back and recovered.

STEP 5 — Volume Confirmation (5min)
  Trigger bar volume > 1.5× average volume (20-period SMA of volume)
  IF volume check fails → SKIP

STEP 6 — Risk:Reward Gate
  FOR LONG:
    Entry = trigger bar close price
    Stop = lowest low during the pullback (below 20-EMA zone)
    Target = entry + 2.5 × ATR(14, 5min)
    R:R = (target - entry) / (entry - stop)
  FOR SHORT:
    Entry = trigger bar close price
    Stop = highest high during the pullback (above 20-EMA zone)
    Target = entry - 2.5 × ATR(14, 5min)
    R:R = (entry - target) / (stop - entry)
  IF R:R < 3.0 → SKIP

STEP 7 — Execute
  Enter at trigger bar close
  Set stop loss at calculated level
  Set target at calculated level

### 2.3 Exit Rules

| Exit Type | Condition | Action |
|-----------|-----------|--------|
| Target hit | Price reaches 2.5×ATR target | Close position, record WIN |
| Stop hit | Price reaches pullback low/high stop | Close position, record LOSS |
| Time stop | 30 bars (150min) elapsed since entry, position not at target or stop | Close at market, record as TIME_EXIT |
| EOD | 15:10 IST | Close all open positions at market |

### 2.4 Position Sizing

- Same as current TradeOS config: slot-based
- ₹1,50,000 per slot, max 6 positions
- Risk per trade: distance to stop × quantity ≤ 1.5% of slot capital (₹2,250)

### 2.5 First Pullback Tracking Logic

The "first pullback" filter is critical to this strategy. Implementation:

State machine per instrument:
  STATE: WAITING_FOR_TREND
    → ADX(14, 15min) crosses above 25 → move to WATCHING_FOR_PULLBACK
    → Reset pullback_count = 0

  STATE: WATCHING_FOR_PULLBACK
    → Price closes on wrong side of 20-EMA(5min) → move to IN_PULLBACK
    → pullback_count += 1

  STATE: IN_PULLBACK
    → Price reclaims 20-EMA(5min) with volume → IF pullback_count == 1: SIGNAL
    → Price reclaims 20-EMA(5min) with volume → IF pullback_count > 1: SKIP (not first)
    → ADX drops below 25 → reset to WAITING_FOR_TREND

  STATE: SIGNAL_FIRED
    → After trade closes (win/loss/time) → move to WATCHING_FOR_PULLBACK
    → ADX drops below 25 → reset to WAITING_FOR_TREND

---

## 3. Strategy S1v3 — Mean Reversion

**Archetype:** Kotegawa "buy the panic" + Bollinger Band oversold + VWAP target
**Edge:** Buy sharp intraday drops when stocks are oversold, sell when price reverts to mean (VWAP).

### 3.1 Timeframes

- **All signals on 15-minute chart**
- 5-minute chart NOT used (mean reversion needs slightly larger candles to filter noise)

### 3.2 Signal Flow

STEP 1 — Time Window Filter
  Valid signals only between 09:30 IST and 14:30 IST
  Before 09:30: market open noise, skip
  After 14:30: not enough time for mean reversion to complete

STEP 2 — Panic Detection
  Calculate day_high = highest price since 09:15 open
  Calculate drop = (day_high - current_price) / day_high
  Calculate drop_threshold = (2 × ATR(14, 15min)) / day_high
  IF drop >= drop_threshold → panic detected, proceed
  IF drop < drop_threshold → NO SIGNAL

STEP 3 — Oversold Confirmation
  RSI(14, 15min) < 30 → oversold confirmed
  AND price <= lower Bollinger Band(20, 2, 15min) → price at statistical extreme
  BOTH conditions must be true → proceed
  Either fails → SKIP

STEP 4 — Reversal Confirmation
  FOR LONG (buying the dip):
    First GREEN candle (close > open) where close > previous candle's high
    This confirms buyers stepping in — not just falling slower
  FOR SHORT (selling the spike):
    Invert all: stock spikes UP > 2×ATR from day low
    RSI(14) > 70 AND price >= upper Bollinger Band
    First RED candle where close < previous candle's low

STEP 5 — Volume Confirmation
  Reversal bar volume > 1.5× average volume (20-period SMA)
  High volume on reversal = institutional participation
  IF volume check fails → SKIP

STEP 6 — Risk:Reward Gate
  FOR LONG:
    Entry = reversal bar close
    Stop = intraday low (lowest low since 09:15)
    Target = current VWAP
    R:R = (VWAP - entry) / (entry - stop)
  FOR SHORT:
    Entry = reversal bar close
    Stop = intraday high
    Target = current VWAP
    R:R = (entry - VWAP) / (stop - entry)
  IF R:R < 2.0 → SKIP (lower threshold than S1v2 because mean reversion has higher win rate)

STEP 7 — Execute
  Enter at reversal bar close
  Set stop loss at intraday low/high
  Set target at VWAP

### 3.3 Exit Rules

| Exit Type | Condition | Action |
|-----------|-----------|--------|
| Target hit | Price reaches VWAP | Close position, record WIN |
| Stop hit | Price reaches intraday low/high | Close position, record LOSS |
| Time stop | 30 bars × 15min = 450min. In practice: if entered at 09:30, time stop = 14:30 EOD. If entered at 12:00, time stop = well past close → becomes EOD exit. | Close at market |
| EOD | 15:10 IST | Close all open positions at market |

**Note:** For S1v3, the time stop is effectively EOD for most entries because 30 × 15min bars = 7.5 hours. The real time constraint is the 14:30 signal window. If no reversion by close, exit.

### 3.4 Position Sizing

- Same pool as S1v2: ₹1,50,000 per slot, max 6 shared
- Risk per trade: distance to stop × quantity ≤ 1.5% of slot capital (₹2,250)

---

## 4. New Indicators Required

| Indicator | Parameters | Used By | Existing? |
|-----------|-----------|---------|-----------|
| EMA(10) | 10-period, 15min | S1v2 directional filter | NEW |
| EMA(20) | 20-period, 5min | S1v2 pullback detection | NEW |
| ADX(14) | 14-period, 15min | S1v2 trend filter | NEW |
| Bollinger Band(20, 2) | 20-period, 2 std dev, 15min | S1v3 oversold/overbought | NEW |
| ATR(14) | 14-period, 5min and 15min | Both — target calc, panic threshold | May exist — verify |
| RSI(14) | 14-period, 15min | S1v3 oversold/overbought | EXISTS (verify timeframe) |
| VWAP | Cumulative from open | S1v3 target | EXISTS |
| Volume SMA(20) | 20-period volume average | Both — volume confirmation | May exist — verify |

---

## 5. Backtester Implementation Plan

### 5.1 Architecture

tools/backtester.py          ← existing engine (1,790 lines)
strategies/
  s1.py                      ← existing S1 (keep for reference)
  s1v2_trend_pullback.py     ← NEW
  s1v3_mean_reversion.py     ← NEW

Each strategy class implements the same interface the backtester expects.

### 5.2 Backtest Parameters

| Parameter | Value |
|-----------|-------|
| Date range | 2025-06-01 to 2026-03-14 (198 trading days) |
| Symbols | All 50 NIFTY 50 stocks in watchlist |
| Capital | ₹9,00,000 (S1 allocation) |
| Max positions | 6 concurrent |
| Slot capital | ₹1,50,000 |
| Commission | ₹20 per order (Zerodha flat fee) |
| Slippage | 0.05% per trade |
| Data intervals needed | 5min AND 15min (S1v2 is multi-timeframe) |

### 5.3 Expected Output

For each strategy, the backtester should report:

- Total P&L (net of commissions + slippage)
- Win rate (%)
- Average win size vs average loss size
- Profit factor (gross wins / gross losses)
- Max drawdown
- Total trades
- Average holding time
- Trades per day average
- Monthly breakdown
- Best/worst single trade
- R:R distribution (histogram of actual R multiples achieved)

### 5.4 Comparison Output

After both strategies complete, a comparison table:

| Metric | S1 (baseline) | S1v2 | S1v3 |
|--------|--------------|------|------|
| Net P&L | -₹X | ? | ? |
| Win Rate | 32% | ? | ? |
| Profit Factor | <1.0 | ? | ? |
| Max Drawdown | ? | ? | ? |
| Avg R:R achieved | ? | ? | ? |

---

## 6. Success Criteria

A strategy is considered **viable for paper trading** if:

| Criterion | Threshold |
|-----------|-----------|
| Net P&L | Positive over 198 days |
| Win rate | ≥ 40% |
| Profit factor | ≥ 1.3 |
| Max drawdown | ≤ 15% of capital |
| Avg R:R achieved | ≥ 2.0 |
| Minimum trades | ≥ 50 (statistical significance) |

If NEITHER strategy meets all criteria → parameter tuning round using optimizer.
If ONE meets criteria → deploy to paper trading, continue testing the other.
If BOTH meet criteria → deploy the better performer, keep the other as secondary.

---

## 7. What This Spec Does NOT Cover (Deferred)

- Live trading integration (signal engine changes)
- HAWK AI filter integration
- Trailing stop implementation
- Regime-adaptive parameter switching
- Multi-strategy portfolio allocation

These are all post-validation. Backtest first, prove profitability, then add complexity.

---

## 8. Decision Log

| # | Decision | Source |
|---|----------|--------|
| D1 | Research scoped to intraday/short-term traders only | TradeOS-03 |
| D2 | Test both S1v2 and S1v3 against backtester | TradeOS-03 |
| D3 | All 15 design parameters locked per recommendations | TradeOS-03 |
| D4 | S1v2: 5min entries, 15min trend filter | TradeOS-03 Q1 |
| D5 | S1v2: Pullback = close below 20-EMA then reclaim | TradeOS-03 Q2 |
| D6 | S1v2: First pullback since ADX crossed 25 | TradeOS-03 Q3 |
| D7 | S1v2: ATR-based target (2.5×ATR) | TradeOS-03 Q4 |
| D8 | S1v2: Time stop 30×5min bars (150min) | TradeOS-03 Q5 |
| D9 | S1v2: ADX is entry filter only | TradeOS-03 Q6 |
| D10 | Both LONG and SHORT in backtest | TradeOS-03 Q7 |
| D11 | S1v3: Panic threshold = 2×ATR adaptive | TradeOS-03 Q8 |
| D12 | S1v3: RSI < 30 for oversold | TradeOS-03 Q9 |
| D13 | S1v3: VWAP as mean-reversion target | TradeOS-03 Q10 |
| D14 | S1v3: Valid window 09:30–14:30 IST | TradeOS-03 Q11 |
| D15 | S1v3: Both directions for mean reversion | TradeOS-03 Q12 |
| D16 | Shared position pool (6 max across both) | TradeOS-03 Q13 |
| D17 | Separate strategy classes (s1v2.py, s1v3.py) | TradeOS-03 Q14 |
| D18 | Add ADX, Bollinger Bands, EMA10, EMA20 to indicators | TradeOS-03 Q15 |
