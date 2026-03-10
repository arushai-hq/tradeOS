"""
Tests for capital allocation validation in run_config_check().

(a) Valid allocation sums pass
(b) Allocation summing to 1.40 raises SystemExit
(c) Allocation summing to 0.80 raises SystemExit
(d) Float tolerance works (0.9999 passes)
"""
from __future__ import annotations

import pytest
import yaml

from main import run_config_check


def _make_configs(tmp_path, allocation: dict):
    """Write settings.yaml + secrets.yaml with given allocation block."""
    settings = {
        "system": {"mode": "paper"},
        "capital": {"total": 500000, "allocation": allocation},
        "risk": {
            "max_loss_per_trade_pct": 0.015,
            "max_daily_loss_pct": 0.03,
            "max_open_positions": 4,
        },
    }
    secrets = {
        "zerodha": {
            "api_key": "test",
            "api_secret": "test",
            "access_token": "test",
            "token_date": "2026-01-01",
        },
        "telegram": {"bot_token": "test", "chat_id": "test"},
    }
    settings_path = tmp_path / "config"
    settings_path.mkdir(exist_ok=True)
    (settings_path / "settings.yaml").write_text(yaml.dump(settings))
    (settings_path / "secrets.yaml").write_text(yaml.dump(secrets))


_VALID = {"s1_intraday": 0.70, "s2_swing": 0.15, "s3_positional": 0.10, "s4_event": 0.05}


def test_valid_allocation_passes(tmp_path, monkeypatch):
    """Allocations summing to exactly 1.00 should pass validation."""
    monkeypatch.chdir(tmp_path)
    _make_configs(tmp_path, _VALID)
    config, _ = run_config_check()
    alloc = config["capital"]["allocation"]
    assert abs(sum(alloc.values()) - 1.0) <= 0.001


def test_allocation_over_1_raises(tmp_path, monkeypatch):
    """Allocations summing to 1.40 must cause sys.exit(1)."""
    monkeypatch.chdir(tmp_path)
    over = {**_VALID, "s1_intraday": 0.70, "s2_swing": 0.30, "s3_positional": 0.30, "s4_event": 0.10}
    _make_configs(tmp_path, over)
    with pytest.raises(SystemExit) as exc_info:
        run_config_check()
    assert exc_info.value.code == 1


def test_allocation_under_1_raises(tmp_path, monkeypatch):
    """Allocations summing to 0.80 must cause sys.exit(1)."""
    monkeypatch.chdir(tmp_path)
    under = {**_VALID, "s1_intraday": 0.50}
    _make_configs(tmp_path, under)
    with pytest.raises(SystemExit) as exc_info:
        run_config_check()
    assert exc_info.value.code == 1


def test_float_tolerance_passes(tmp_path, monkeypatch):
    """Allocations summing to 0.9999 (within ±0.001 tolerance) should pass."""
    monkeypatch.chdir(tmp_path)
    near = {**_VALID, "s1_intraday": 0.6999}
    _make_configs(tmp_path, near)
    config, _ = run_config_check()
    alloc = config["capital"]["allocation"]
    total = sum(alloc.values())
    assert abs(total - 1.0) <= 0.001
