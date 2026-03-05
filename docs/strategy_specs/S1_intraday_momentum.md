# S1 — Intraday Momentum Strategy Spec

## Overview
Ride institutional order flow momentum in the first 60–90 minutes after market open.

## Timeframe
15-minute candles | Trade window: 9:15 AM – 3:00 PM IST

## Capital Allocation
30% of total capital (₹1.5L at ₹5L base)

## Entry Conditions

### Long (Buy)
- 9 EMA crosses ABOVE 21 EMA on 15-min candle
- Current volume > 1.5x of 20-period average volume
- Price is ABOVE VWAP
- RSI between 55–70

### Short (Sell)
- 9 EMA crosses BELOW 21 EMA on 15-min candle
- Current volume > 1.5x of 20-period average volume
- Price is BELOW VWAP
- RSI between 30–45

## Exit Conditions
- Stop-loss: Below previous 15-min swing low (long) / above swing high (short)
- Target: 1:2 Risk-Reward minimum
- Hard exit: 3:00 PM IST (no overnight positions)
- Signal reversal: EMA cross in opposite direction = immediate exit

## Indicator Roles
| Indicator | Role | Filters |
|-----------|------|---------|
| EMA 9/21 | Trend direction | Sideways chop |
| Volume 1.5x | Conviction | Fake breakouts |
| VWAP | Institutional anchor | Trading against smart money |
| RSI 55–70 | Momentum quality | Exhausted/overbought moves |

## Risk Parameters
- Max loss per trade: 1.5% of S1 capital = ₹2,250
- Max trades per day: 3
- Min Risk-Reward: 1:2

## Universe
20 NIFTY 50 stocks with avg daily volume > 50L shares
(See config/settings.yaml watchlist)

## Status
🟡 Spec complete. Coding pending Phase 1 build.
