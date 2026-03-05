# Layer 3 — Simulation Thresholds (backtesting.py)

## Data Requirements

- **Minimum**: 1 year of NSE historical OHLCV data at 15-minute resolution
- **Source**: Zerodha KiteConnect `kite.historical_data()` API
- **Instruments**: NIFTY 50 watchlist stocks (see `config/settings.yaml`)
- **Tool**: `backtesting.py` library

Use `asyncio.to_thread(kite.historical_data, ...)` when fetching — it's a blocking call.

## The 7 Mandatory Thresholds

All 7 must be met simultaneously. Failing any single one fails Layer 3.

| Metric | Threshold | Why It Matters |
|--------|-----------|----------------|
| Sharpe Ratio | > 1.2 | Risk-adjusted return quality. Below 1.0 means the return doesn't justify the risk taken. |
| Win Rate | > 45% | Minimum edge. With 1:2 RR, even 45% win rate is profitable, but below this suggests no edge. |
| Max Drawdown | < 15% | Capital preservation. 15% drawdown on ₹50K = ₹7,500 loss from peak. Beyond this is psychologically unsustainable. |
| Profit Factor | > 1.5 | Gross profit / gross loss. 1.5 means you make ₹1.50 for every ₹1.00 lost. |
| Average RR Achieved | > 1.5 | Actual trades should achieve better than 1.5:1 on average. The 1:2 minimum is the floor. |
| Total Trades | >= 100 | Statistical significance. Fewer than 100 trades could be luck. |
| Max Consecutive Losses | < 8 | Psychological survivability and kill switch interaction. 8 losses trigger Level 1 kill switch at loss 3, Level 2 at daily loss breach — this should never reach 8. |

## Extracting Metrics from backtesting.py

```python
from backtesting import Backtest, Strategy

bt = Backtest(data, S1Strategy, cash=150000, commission=0.0003)
stats = bt.run()

sharpe = stats["Sharpe Ratio"]
win_rate = stats["Win Rate [%]"] / 100
max_drawdown = abs(stats["Max. Drawdown [%]"]) / 100
profit_factor = stats["Profit Factor"]
total_trades = stats["# Trades"]
avg_rr = stats["Avg. Trade [%]"] / abs(stats["Avg. Loss [%]"])  # approximate
```

## Additional Simulation Tests

### Monte Carlo (tests/simulation/test_monte_carlo.py)

Run 1000 trade sequences by sampling with replacement from the historical trade outcomes. This tests whether the performance is robust or luck-dependent.

```python
import numpy as np

def run_monte_carlo(trade_returns: list[float], n_simulations: int = 1000) -> dict:
    """
    trade_returns: list of individual trade P&L percentages from backtest
    Returns dict with percentile statistics.
    """
    results = {"max_drawdowns": [], "total_returns": []}

    for _ in range(n_simulations):
        sequence = np.random.choice(trade_returns, size=len(trade_returns), replace=True)
        cumulative = np.cumprod(1 + sequence / 100)
        running_max = np.maximum.accumulate(cumulative)
        drawdown = (cumulative - running_max) / running_max
        results["max_drawdowns"].append(abs(drawdown.min()))
        results["total_returns"].append((cumulative[-1] - 1) * 100)

    return results
```

Gate conditions for Monte Carlo:
- `np.percentile(results["max_drawdowns"], 95) < 0.20` — worst 5% of scenarios, drawdown under 20%
- `np.percentile(results["total_returns"], 5) > 0` — even the worst 5% of sequences are profitable

### Kill Switch Impact Simulation (tests/simulation/test_kill_switch_sim.py)

Run the S1 backtest twice — once with kill switch logic disabled, once enabled. Compare max drawdown.

```python
def test_kill_switch_reduces_drawdown():
    stats_no_ks = bt_no_killswitch.run()
    stats_with_ks = bt_with_killswitch.run()

    drawdown_no_ks = abs(stats_no_ks["Max. Drawdown [%]"]) / 100
    drawdown_with_ks = abs(stats_with_ks["Max. Drawdown [%]"]) / 100

    # Kill switch must cut max drawdown by more than half
    assert drawdown_with_ks < drawdown_no_ks * 0.5, (
        f"Kill switch should reduce drawdown by >50%. "
        f"Without KS: {drawdown_no_ks:.1%}, With KS: {drawdown_with_ks:.1%}"
    )

    # Log both for audit trail
    log.info("kill_switch_simulation_complete",
             drawdown_no_ks=drawdown_no_ks,
             drawdown_with_ks=drawdown_with_ks,
             reduction_pct=(1 - drawdown_with_ks / drawdown_no_ks) * 100)
```

This simulation provides empirical evidence that the kill switch earns its complexity cost. If it doesn't reduce drawdown by >50%, the kill switch thresholds need tuning, not removing.

## Writing the Layer 3 Test

The `test_s1_backtest.py` test should:
1. Fetch 1 year of data (or load from cache)
2. Run `Backtest.run()`
3. Assert all 7 thresholds with descriptive failure messages
4. Log the full stats dict for audit trail
5. NOT lower a threshold when a strategy "almost" passes

Failure message pattern:
```python
assert sharpe > 1.2, (
    f"Sharpe Ratio {sharpe:.3f} below threshold 1.2. "
    f"Strategy lacks sufficient risk-adjusted returns. "
    f"Review entry/exit conditions before retrying."
)
```

The error message should explain what the failure means, not just report the number. Someone reading the failure at 2 AM should immediately understand what to investigate.
