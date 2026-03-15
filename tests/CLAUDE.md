# tests/ — Test Suite

Unit tests and integration tests for all TradeOS modules.

## Skills

| Skill | When to use |
|-------|-------------|
| tradeos-testing | Test standards, conventions, regression rules |
| tradeos-test-pyramid | D8: Three-layer testing gate |

## Commands

```bash
python -m pytest                         # Full suite
python -m pytest tests/unit/ -v          # Unit tests only
python -m pytest tests/integration/      # Integration tests
python -m pytest -k "test_name"          # Specific test
python -m pytest -x -q                   # Stop on first failure, quiet
```

## Conventions

- All tests must pass before commit (499 passing = quality gate)
- Integration tests require `TRADEOS_TEST_DB_DSN` env var
- Every new module gets a corresponding test file
- Minimum coverage: happy path, kill switch blocks, bad input rejected
- Tests mirror module structure: `tests/unit/test_{module}.py`
