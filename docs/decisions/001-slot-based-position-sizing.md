# ADR-001: Slot-Based Position Sizing

**Date:** 2026-03-06
**Status:** Accepted
**Deciders:** Irfan (Arushai Systems)

## Context

TradeOS needed capital management beyond fixed-risk percentage sizing. Deep Nemawashi analysis covered 15 edge cases, real Zerodha charge calculations (STT, brokerage, GST, stamp duty, SEBI fees), and a comparison of three approaches: percentage-of-equity, fixed-fractional, and slot-based.

Key constraints:
- ₹10L paper trading capital across 4 strategy slots (S1=70%, S2=15%, S3=10%, S4=5%)
- Maximum 4 concurrent positions for S1
- Need protection against cascading losses in volatile sessions
- Position sizes must account for actual trading charges

## Decision

Adopt **slot-based 3-layer position sizing**:

1. **Risk-based shares** — Calculate quantity from stop distance and per-trade risk (1.5% of slot capital)
2. **Capital cap scale-down** — If position value exceeds slot capital (₹1,75,000 / 4 = ₹43,750 per slot), scale down proportionally
3. **Viability floors** — Reject if actual risk < ₹1,000 (min_risk_floor) or position value < ₹15,000 (min_position_value)

Configuration:
- 4 slots at ~₹43,750 each (S1 allocation: ₹7,00,000 / 4 positions)
- 1.5% risk per slot = ₹2,625 max risk per trade
- ₹1,000 minimum risk floor
- No-entry after 14:45 IST (configurable via `trading_hours.no_entry_after`)
- Startup validation: refuse if slot_capital < ₹40,000

## Consequences

- More conservative than percentage-of-equity but protects against cascading losses
- Charge estimation logged per sized position for transparency
- Requires allocation sum validation at startup (must equal 1.00)
- Pending orders cancelled at hard_exit before emergency_exit_all
- Stop floor at 2% prevents sizer rejection on tight swing stops
