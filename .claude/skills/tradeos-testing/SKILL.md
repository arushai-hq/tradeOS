---
name: tradeos-testing
description: >
  Testing standards and patterns for TradeOS. Use whenever writing or modifying
  tests, checking test coverage, or understanding the test organization.
  Invoke for test writing, test fixing, test coverage analysis, adding regression
  tests for bug fixes, or understanding test conventions.
  Do NOT invoke for: general pytest patterns outside TradeOS, non-TradeOS projects,
  or production code changes that don't involve tests.
related-skills: tradeos-test-pyramid, test-master, tradeos-gotchas
---

# TradeOS Testing Standards

## Current Benchmark

| Metric | Value |
|--------|-------|
| Total tests | 499+ (as of commit `6091ed2`) |
| Failures | 0 (zero-failure target) |
| Skipped | 12 |
| Framework | pytest |
| Target | ALL tests pass before ANY commit |

## Running Tests

```bash
# Via CLI (preferred in production)
tradeos test -x -q

# Via pytest directly (development)
python -m pytest tests/ -x -q

# Single module
python -m pytest tests/test_risk_manager/ -x -q

# Single test file
python -m pytest tests/test_risk_manager/test_kill_switch.py -x -q

# With verbose output
python -m pytest tests/ -v
```

## Test Organization

Tests mirror the module structure:

```
tests/
├── test_data_engine/
│   ├── test_tick_validator.py
│   ├── test_ws_manager.py
│   └── test_tick_storage.py
├── test_strategy_engine/
│   ├── test_candle_builder.py
│   ├── test_indicators.py
│   └── test_s1_signal_generator.py
├── test_risk_manager/
│   ├── test_kill_switch.py
│   ├── test_position_sizer.py
│   └── test_pnl_tracker.py
├── test_execution_engine/
│   ├── test_order_state_machine.py
│   └── test_paper_broker.py
├── test_regime_detector/
├── test_tools/
├── test_utils/
└── conftest.py
```

## Test Coverage Requirements

Every new module MUST have tests covering:

1. **Happy path** — normal operation works correctly
2. **Kill switch blocks** — operations blocked when kill switch active
3. **Bad input rejection** — invalid data handled gracefully
4. **Edge cases** — boundary conditions (empty data, zero values, negative qty)

## Bug Fix Testing Rule

> **Every bug fix MUST add new test case(s) that would have caught the bug.**

This is how we went from 222 tests to 499+. The B1-B14 bug catalogue (see `tradeos-gotchas` skill)
each added regression tests. This is non-negotiable.

Examples:
- B7 (field name mismatch) → test that unrealized P&L uses `avg_price` not `entry_price`
- B8 (ghost positions) → test that no ghost LONG appears after exit fill
- B10 (pre-market spam) → test that warnings are DEBUG before 09:15

## Test Patterns

### Mocking Zerodha API
```python
# Always mock kite API calls — never hit real broker in tests
mock_kite = MagicMock()
mock_kite.positions.return_value = {"day": [...]}
mock_kite.orders.return_value = [...]
```

### Time-Dependent Tests
```python
# Always freeze time for time-dependent logic
from unittest.mock import patch
import datetime

with patch('utils.time_utils.get_ist_now') as mock_now:
    mock_now.return_value = datetime.datetime(2026, 3, 10, 14, 30,
                                               tzinfo=pytz.timezone("Asia/Kolkata"))
    # Test logic here
```

### Async Tests
```python
import pytest

@pytest.mark.asyncio
async def test_async_operation():
    result = await some_async_function()
    assert result == expected
```

### Position Accounting Tests
```python
# ALWAYS test both LONG and SHORT positions
# SHORT uses negative qty — this has caused critical bugs (B7)
long_position = {"symbol": "RELIANCE", "qty": 100, "avg_price": 2500.0, "side": "BUY"}
short_position = {"symbol": "RELIANCE", "qty": -100, "avg_price": 2500.0, "side": "SELL"}
```

## Pre-Commit Checklist

1. Run full suite: `python -m pytest tests/ -x -q`
2. Verify zero failures
3. Check that new code has corresponding tests
4. Check that bug fixes have regression tests
5. Verify no tests depend on real broker/DB connections
