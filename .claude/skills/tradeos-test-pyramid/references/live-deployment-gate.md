# Live Deployment Gate

## What This Gate Is

The live deployment gate is the single decision point before real capital (₹50,000) is deployed on NSE. It aggregates evidence from all 3 layers and produces a binary decision: open or closed.

The gate function **never modifies `config/settings.yaml` itself**. It returns a decision. The human operator makes the final change and presses the button. This separation of concerns means the gate can be called repeatedly to check readiness without risk.

## The 10 Gate Conditions

```python
gate_open = (
    layer1_unit_tests_passing    == True   # pytest exits with code 0
    AND layer1_coverage_pct      >= 90     # coverage.py report on risk_manager/ data_engine/ strategies/s1_intraday/
    AND layer2_paper_weeks       >= 3      # calendar weeks of paper trading
    AND layer2_all_criteria_met  == True   # all 5 Layer 2 criteria simultaneously met
    AND layer3_sharpe            > 1.2
    AND layer3_win_rate          > 0.45
    AND layer3_max_drawdown      < 0.15
    AND layer3_profit_factor     > 1.5
    AND layer3_min_trades        >= 100
    AND layer3_monte_carlo_p95   < 0.20   # 95th percentile drawdown across 1000 sequences
)
```

## Implementation Pattern

```python
from dataclasses import dataclass, field

@dataclass
class GateResult:
    open: bool
    failed_conditions: list[str] = field(default_factory=list)
    passed_conditions: list[str] = field(default_factory=list)

class LiveGateClosedError(Exception):
    """Raised when live gate check fails — carries the list of failed conditions."""
    def __init__(self, failed_conditions: list[str]):
        self.failed_conditions = failed_conditions
        super().__init__(
            f"Live gate CLOSED. Failed conditions: {', '.join(failed_conditions)}"
        )

def check_live_gate(
    layer1_passing: bool,
    layer1_coverage: float,
    layer2_weeks: int,
    layer2_criteria_met: bool,
    layer3_sharpe: float,
    layer3_win_rate: float,
    layer3_max_drawdown: float,
    layer3_profit_factor: float,
    layer3_trade_count: int,
    layer3_monte_carlo_p95: float,
) -> GateResult:
    """
    Returns GateResult. Does NOT modify config/settings.yaml.
    Caller is responsible for the actual mode change.
    """
    checks = [
        (layer1_passing,                "layer1_unit_tests_passing"),
        (layer1_coverage >= 90,         f"layer1_coverage >= 90% (actual: {layer1_coverage:.1f}%)"),
        (layer2_weeks >= 3,             f"layer2_paper_weeks >= 3 (actual: {layer2_weeks})"),
        (layer2_criteria_met,           "layer2_all_criteria_met"),
        (layer3_sharpe > 1.2,           f"sharpe > 1.2 (actual: {layer3_sharpe:.3f})"),
        (layer3_win_rate > 0.45,        f"win_rate > 45% (actual: {layer3_win_rate:.1%})"),
        (layer3_max_drawdown < 0.15,    f"max_drawdown < 15% (actual: {layer3_max_drawdown:.1%})"),
        (layer3_profit_factor > 1.5,    f"profit_factor > 1.5 (actual: {layer3_profit_factor:.3f})"),
        (layer3_trade_count >= 100,     f"min_trades >= 100 (actual: {layer3_trade_count})"),
        (layer3_monte_carlo_p95 < 0.20, f"monte_carlo_p95 < 20% (actual: {layer3_monte_carlo_p95:.1%})"),
    ]

    failed = [label for condition, label in checks if not condition]
    passed = [label for condition, label in checks if condition]

    result = GateResult(open=len(failed) == 0, failed_conditions=failed, passed_conditions=passed)

    if result.open:
        log.info("live_gate_open",
                 all_conditions_met=True,
                 next_step="change config/settings.yaml mode: paper → live, capital: 50000")
        # Telegram notification — operator action required
        send_telegram_alert(level="INFO",
                           message="🟢 LIVE GATE OPEN — all conditions met. Deploy when ready.")
    else:
        log.warning("live_gate_closed",
                   failed_count=len(failed),
                   failed_conditions=failed)
        for condition in failed:
            log.warning("live_gate_condition_failed", condition=condition)

    return result
```

## What Happens When Gate Opens

The operator should:
1. Review the gate report
2. Manually edit `config/settings.yaml`: change `mode: paper` → `mode: live`, set `capital.total: 50000`
3. Commit the change with a note: `config: open live gate — all D8 criteria met`
4. Deploy

The system does not auto-deploy. The human presses the button.

## What Happens When Gate Is Closed

Log which conditions failed. Do NOT lower thresholds to force a pass. Instead:

- `layer1_coverage < 90` → identify which modules are under-covered and write missing tests
- `layer2_weeks < 3` → wait longer. There is no shortcut.
- `layer2_criteria_met == False` → fix the specific criterion that failed (identified in observation log)
- `layer3_sharpe < 1.2` → the strategy needs tuning, not the threshold
- `layer3_max_drawdown >= 0.15` → review position sizing and stop-loss placement
- `layer3_trade_count < 100` → extend the backtest period or widen the universe

The point of the gate is to protect against the risk that feels obvious in hindsight: "it almost passed, just a few tweaks." Knight Capital lost $440M in 45 minutes with a strategy that "almost worked." Almost is not good enough with real capital.

## Never Lower a Threshold

If the strategy doesn't meet a threshold, the answer is to improve the strategy or collect more data — not to change the threshold. Changing thresholds to make a failing strategy pass is the most dangerous anti-pattern in algorithmic trading. Document this reasoning in any test file that defines these constants, so future maintainers understand why the numbers are what they are.
