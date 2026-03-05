# TradeOS — Arushai Systems Private Limited

AI-powered systematic trading system for Indian markets (NSE/BSE).

## Architecture
```
Data Engine → Strategy Engine → Risk Manager → Execution Engine
```

## 4 Sub-Strategies
| ID | Style | Timeframe | Capital Allocation |
|----|-------|-----------|-------------------|
| S1 | Intraday Momentum | 15-min | 30% |
| S2 | Swing Mean Reversion | Daily | 30% |
| S3 | Positional Trend Follow | Weekly | 30% |
| S4 | Event-Driven | As needed | 10% |

## Build Phases
- **Phase 1 (Active):** Data Engine + S1 Intraday + Paper Trading
- **Phase 2:** S2 Swing + Live ₹50K deployment
- **Phase 3:** S3 + S4 + Full capital scaling

## Broker
Zerodha KiteConnect API — NSE Equities (Phase 1)

## Risk Rules (Non-Negotiable)
- Max loss per trade: 1.5% of allocated capital
- Max daily loss: 3% of total capital
- Max open positions: 3
- Stop-loss mandatory on every order
- Hard intraday exit: 3:00 PM IST

## Status
🟡 Phase 1 — Brainstorming & Architecture Complete. Build in progress.
