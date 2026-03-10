"""
Tests for minimum slot capital validation in run_config_check().

(a) Slot capital ≥ ₹40,000 passes validation
(b) Slot capital < ₹40,000 raises SystemExit
"""
from __future__ import annotations

import pytest
import yaml

from main import run_config_check


_VALID_ALLOC = {"s1_intraday": 0.70, "s2_swing": 0.15, "s3_positional": 0.10, "s4_event": 0.05}


def _make_configs(tmp_path, total: int = 500000, allocation: dict | None = None,
                  max_positions: int = 4, min_slot_capital: int | None = None):
    """Write settings.yaml + secrets.yaml with given capital/risk parameters."""
    alloc = allocation or _VALID_ALLOC
    settings = {
        "system": {"mode": "paper"},
        "capital": {"total": total, "allocation": alloc},
        "risk": {
            "max_loss_per_trade_pct": 0.015,
            "max_daily_loss_pct": 0.03,
            "max_open_positions": max_positions,
        },
    }
    if min_slot_capital is not None:
        settings["position_sizing"] = {"min_slot_capital": min_slot_capital}
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


def test_slot_capital_above_minimum_passes(tmp_path, monkeypatch):
    """
    total=500000, s1=0.70, max_positions=4 → slot_capital=87500 ≥ 40000.
    Validation passes.
    """
    monkeypatch.chdir(tmp_path)
    _make_configs(tmp_path)
    config, _ = run_config_check()
    # Verify slot_capital calculation
    total = config["capital"]["total"]
    s1 = config["capital"]["allocation"]["s1_intraday"]
    max_pos = config["risk"]["max_open_positions"]
    slot = (total * s1) / max_pos
    assert slot >= 40000


def test_slot_capital_below_minimum_raises(tmp_path, monkeypatch):
    """
    total=100000, s1=0.70, max_positions=4 → slot_capital=17500 < 40000.
    Must cause sys.exit(1).
    """
    monkeypatch.chdir(tmp_path)
    _make_configs(tmp_path, total=100000)
    with pytest.raises(SystemExit) as exc_info:
        run_config_check()
    assert exc_info.value.code == 1
