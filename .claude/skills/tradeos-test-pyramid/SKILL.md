---
name: tradeos-test-pyramid
description: >
  TradeOS D8 testing pyramid enforcer — the three-layer gate that must be fully cleared
  before any live capital is deployed on NSE/BSE. Use this skill whenever implementing:
  unit tests for kill switch, tick validator, order state machine, risk manager, or S1
  strategy; writing pytest fixtures for TradeOS components; setting up the conftest.py
  shared fixture suite; writing parametrized boundary tests for RSI/loss/position limits;
  implementing the Layer 2 integration test criteria checklist; writing the backtesting.py
  simulation that validates S1 against 7 performance thresholds; implementing the Monte
  Carlo drawdown simulation; writing the kill switch impact simulation; implementing the
  live deployment gate function; or deciding whether the system is ready to go live.
  Invoke for tasks like: "write unit tests for the kill switch", "implement the S1
  backtesting validation", "set up pytest fixtures for TradeOS", "write the live
  deployment gate check", "run Monte Carlo simulation on S1 strategy", "what tests need
  to pass before going live", "write integration tests for TradeOS paper trading",
  "parametrize RSI boundary tests", "verify system is ready for live deployment",
  "write the conftest.py for TradeOS tests". Do NOT invoke for: pytest setup in
  Django/Flask apps, data science project testing, generic CI/CD pipelines, REST API
  integration tests, or any testing context outside TradeOS.
related-skills: test-master, python-pro, pandas-pro, tradeos-kill-switch-guardian, tradeos-order-state-machine, tradeos-tick-validator, tradeos-async-architecture, tradeos-position-reconciler, tradeos-observability
---

# TradeOS Testing Pyramid (D8)

## Cardinal Rule
No layer can be skipped. No gate can be lowered. All 3 layers must pass — simultaneously — before any live capital is deployed. "Almost passed" means "did not pass."

## The 3 Layers

| Layer | Tool | Minimum Duration | Gate Condition |
|-------|------|-----------------|----------------|
| Layer 1 | pytest | < 60s to run | > 90% coverage, all mandatory tests present |
| Layer 2 | Paper trading | 3 weeks minimum | All 5 criteria met with zero manual interventions |
| Layer 3 | backtesting.py | 1 year NSE data | All 7 thresholds met + Monte Carlo passes |

Layer 1 must pass before Layer 2 begins. Layer 2 must pass before Layer 3 runs. Layer 3 must pass before live gate opens.

## Reference Files

Read the relevant file for the task at hand:

| Task | Read |
|------|------|
| Writing unit tests (which tests are mandatory) | `references/layer1-unit-test-catalogue.md` |
| Setting up conftest.py, fixtures, mocking rules | `references/pytest-conventions.md` |
| Paper trade gate criteria (what constitutes Layer 2 pass) | `references/layer2-integration-criteria.md` |
| Backtest thresholds, Monte Carlo, kill switch simulation | `references/layer3-simulation-thresholds.md` |
| Live deployment gate function, gate conditions | `references/live-deployment-gate.md` |

## Test File Structure

```
tests/
├── conftest.py                          (shared fixtures — all 5 mandatory)
├── unit/
│   ├── test_tick_validator.py           (11 tests)
│   ├── test_kill_switch.py              (10 tests)
│   ├── test_order_state_machine.py      (10 tests)
│   ├── test_risk_manager.py             (6 tests)
│   └── test_s1_strategy.py             (10 tests)
├── integration/
│   ├── test_ws_reconnect.py
│   ├── test_reconciliation.py
│   ├── test_pnl_accuracy.py
│   └── test_order_lifecycle.py
└── simulation/
    ├── test_s1_backtest.py              (7 Layer 3 thresholds)
    ├── test_monte_carlo.py              (1000 sequences)
    └── test_kill_switch_sim.py         (drawdown comparison)
```

## Starting Point

When the user asks to write any test, first read `references/layer1-unit-test-catalogue.md` to identify which mandatory test cases belong in that file. For fixture setup, read `references/pytest-conventions.md`. For Layer 3, read `references/layer3-simulation-thresholds.md` — the thresholds are non-negotiable.
