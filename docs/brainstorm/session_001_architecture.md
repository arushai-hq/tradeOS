# Brainstorm Session 001 — System Architecture

**Date:** 2026-03-05
**Status:** Complete

## Decisions Made

### Capital
- Total: ₹5L
- Phase 1 live deployment: ₹50K (S1 only)
- Scaling: Post 6–8 week paper trade validation

### Broker
- Zerodha KiteConnect API
- NSE Equities only (Phase 1)
- F&O deferred to Phase 3 (capital insufficient at ₹5L)

### Strategy Allocation
| Strategy | Allocation | Phase |
|----------|-----------|-------|
| S1 Intraday Momentum | 30% | Phase 1 |
| S2 Swing Mean Reversion | 30% | Phase 2 |
| S3 Positional Trend | 30% | Phase 3 |
| S4 Event-Driven | 10% | Phase 3 |

### Risk Rules (Non-Negotiable)
- Max loss/trade: 1.5% allocated capital
- Max daily loss: 3% total capital
- Max open positions: 3
- Stop-loss mandatory on every order
- Hard intraday exit: 3:00 PM IST

### Build Sequence (3-2-1)
1. Step 3: Understand S1 strategy logic ✅
2. Step 2: Full project folder structure ← Current
3. Step 1: Code the Data Engine ← Next

### Infrastructure
- Paper trade: 6–8 weeks minimum
- Deployment: VPS (TBD — Hetzner recommended for cost)
- Language: Python
- Mode switch: config/settings.yaml → mode: paper | live

## Open Decisions
- [ ] VPS selection (local vs cloud)
- [ ] S2, S3, S4 strategy specs
- [ ] Backtesting data source (KiteConnect historical vs NSEpy)
