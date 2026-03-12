"""
HAWK — Unit tests for Multi-Model Consensus Mode.

Tests:
  (a) 4 models all pick same stock same direction → UNANIMOUS
  (b) 3 of 4 agree → STRONG
  (c) 2 of 4 agree → MAJORITY
  (d) 1 model fails, 3 produce results → consensus from 3
  (e) All models fail → graceful empty result
  (f) Consensus score calculation correct (votes × conviction weight)
  (g) Entry zone averaging correct
  (h) CLI --consensus flag works
  (i) CLI --single overrides consensus.enabled=true
  (j) JSON output has correct consensus structure
  (k) Telegram consensus format correct
"""
from __future__ import annotations

import os
import sys

import pytest

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pick(
    symbol: str = "RELIANCE",
    direction: str = "LONG",
    conviction: str = "HIGH",
    entry_zone: list | None = None,
    reasoning: str = "Test reasoning",
    rank: int = 1,
) -> dict:
    return {
        "rank": rank,
        "symbol": symbol,
        "direction": direction,
        "conviction": conviction,
        "entry_zone": entry_zone or [2850, 2880],
        "support": 2800,
        "resistance": 2950,
        "reasoning": reasoning,
        "risk_flag": None,
    }


def _model_result(
    name: str,
    picks: list[dict],
    cost: float = 0.01,
    tokens_in: int = 500,
    tokens_out: int = 200,
    elapsed: float = 10.0,
) -> dict:
    return {
        "name": name,
        "model_id": f"provider/{name.lower()}",
        "picks": picks,
        "metadata": {
            "cost_usd": cost,
            "tokens_input": tokens_in,
            "tokens_output": tokens_out,
            "elapsed_s": elapsed,
        },
    }


# ---------------------------------------------------------------------------
# (a) 4 models all pick same stock → UNANIMOUS
# ---------------------------------------------------------------------------

def test_unanimous_four_models_same_pick():
    """4 models all pick the same (symbol, direction) → UNANIMOUS tag."""
    from tools.hawk_engine.consensus import build_consensus

    model_results = [
        _model_result(f"Model{i}", [_pick("HCLTECH", "SHORT", "HIGH",
                                           entry_zone=[1380, 1395],
                                           reasoning=f"Model {i} reasoning")])
        for i in range(4)
    ]
    result = build_consensus(model_results, total_models=4)

    unanimous = [p for p in result["consensus_picks"] if p["consensus_tag"] == "UNANIMOUS"]
    assert len(unanimous) == 1
    assert unanimous[0]["symbol"] == "HCLTECH"
    assert unanimous[0]["direction"] == "SHORT"
    assert unanimous[0]["model_votes"] == 4
    # Score: 4 models × avg_weight(3.0 for all HIGH) = 12.0
    assert unanimous[0]["consensus_score"] == 12.0
    assert unanimous[0]["avg_conviction"] == "HIGH"


# ---------------------------------------------------------------------------
# (b) 3 of 4 agree → STRONG
# ---------------------------------------------------------------------------

def test_strong_three_of_four():
    """3 of 4 models agree on same stock → STRONG tag."""
    from tools.hawk_engine.consensus import build_consensus

    common = _pick("INFY", "LONG", "MEDIUM", [1500, 1510], "IT momentum")
    different = _pick("TCS", "LONG", "HIGH", [4200, 4230], "Tech leader")

    model_results = [
        _model_result(f"M{i}", [common]) for i in range(3)
    ] + [
        _model_result("M3", [different]),
    ]
    result = build_consensus(model_results, total_models=4)

    infy = [p for p in result["consensus_picks"] if p["symbol"] == "INFY"]
    assert len(infy) == 1
    assert infy[0]["consensus_tag"] == "STRONG"
    assert infy[0]["model_votes"] == 3


# ---------------------------------------------------------------------------
# (c) 2 of 4 agree → MAJORITY
# ---------------------------------------------------------------------------

def test_majority_two_of_four():
    """2 of 4 models agree on same stock → MAJORITY tag."""
    from tools.hawk_engine.consensus import build_consensus

    model_results = [
        _model_result("M0", [_pick("AXISBANK", "SHORT", "HIGH", [1285, 1295])]),
        _model_result("M1", [_pick("AXISBANK", "SHORT", "MEDIUM", [1280, 1290])]),
        _model_result("M2", [_pick("SUNPHARMA", "LONG", "HIGH", [1800, 1815])]),
        _model_result("M3", [_pick("RELIANCE", "LONG", "MEDIUM", [2850, 2880])]),
    ]
    result = build_consensus(model_results, total_models=4)

    axis = [p for p in result["consensus_picks"] if p["symbol"] == "AXISBANK"]
    assert len(axis) == 1
    assert axis[0]["consensus_tag"] == "MAJORITY"
    assert axis[0]["model_votes"] == 2


# ---------------------------------------------------------------------------
# (d) 1 model fails, 3 produce results → consensus from 3
# ---------------------------------------------------------------------------

def test_one_model_fails_consensus_from_remaining():
    """1 model fails (not in results), consensus built from 3 remaining."""
    from tools.hawk_engine.consensus import build_consensus

    pick = _pick("RELIANCE", "LONG", "HIGH", [2850, 2880], "Energy play")
    model_results = [
        _model_result(f"M{i}", [pick]) for i in range(3)
    ]
    # 4th model failed — not in model_results
    result = build_consensus(model_results, total_models=4)

    assert len(result["models_used"]) == 3
    assert result["total_models"] == 4
    rel = [p for p in result["consensus_picks"] if p["symbol"] == "RELIANCE"]
    assert len(rel) == 1
    assert rel[0]["model_votes"] == 3
    # 3/3 successful = unanimous
    assert rel[0]["consensus_tag"] == "UNANIMOUS"


# ---------------------------------------------------------------------------
# (e) All models fail → graceful empty result
# ---------------------------------------------------------------------------

def test_all_models_fail_empty_consensus():
    """All models fail → empty consensus_picks."""
    from tools.hawk_engine.consensus import build_consensus

    result = build_consensus([], total_models=4)
    assert result["consensus_picks"] == []
    assert result["models_used"] == []
    assert result["total_models"] == 4
    assert result["mode"] == "consensus"
    assert result["aggregate_metadata"]["total_cost_usd"] == 0


# ---------------------------------------------------------------------------
# (f) Consensus score calculation (votes × conviction weight)
# ---------------------------------------------------------------------------

def test_consensus_score_mixed_convictions():
    """3 HIGH + 1 LOW = score 4 × (3+3+3+1)/4 = 4 × 2.5 = 10.0."""
    from tools.hawk_engine.consensus import build_consensus

    model_results = []
    for i, conv in enumerate(["HIGH", "HIGH", "HIGH", "LOW"]):
        model_results.append(
            _model_result(f"M{i}", [_pick("TCS", "LONG", conv, [4200, 4230],
                                          f"Reason {i}")])
        )
    result = build_consensus(model_results, total_models=4)

    tcs = [p for p in result["consensus_picks"] if p["symbol"] == "TCS"]
    assert len(tcs) == 1
    assert tcs[0]["consensus_score"] == 10.0
    # avg_weight = 2.5, which maps to HIGH (>= 2.5)
    assert tcs[0]["avg_conviction"] == "HIGH"


# ---------------------------------------------------------------------------
# (g) Entry zone averaging
# ---------------------------------------------------------------------------

def test_entry_zone_averaging():
    """Entry zones from 4 models are averaged correctly."""
    from tools.hawk_engine.consensus import build_consensus

    zones = [[460, 470], [465, 475], [462, 472], [468, 478]]
    model_results = [
        _model_result(f"M{i}", [_pick("SUNPHARMA", "LONG", "HIGH",
                                       entry_zone=zone, reasoning=f"R{i}")])
        for i, zone in enumerate(zones)
    ]
    result = build_consensus(model_results, total_models=4)

    sun = [p for p in result["consensus_picks"] if p["symbol"] == "SUNPHARMA"]
    assert len(sun) == 1
    # mean(460,465,462,468) = 463.75, mean(470,475,472,478) = 473.75
    assert sun[0]["entry_zone"][0] == pytest.approx(463.75)
    assert sun[0]["entry_zone"][1] == pytest.approx(473.75)


# ---------------------------------------------------------------------------
# (h) CLI --consensus flag works
# ---------------------------------------------------------------------------

def test_cli_consensus_flag_activates_consensus(tmp_path):
    """--consensus CLI flag forces consensus mode even when config disabled."""
    from unittest.mock import patch, MagicMock

    mock_consensus = MagicMock(return_value=0)
    mock_single = MagicMock(return_value=0)

    with patch("tools.hawk.run_evening_consensus", mock_consensus), \
         patch("tools.hawk.run_evening", mock_single), \
         patch("tools.hawk_engine.config.load_hawk_config", return_value={
             "consensus": {"enabled": False},
             "output_dir": str(tmp_path),
         }), \
         patch("tools.hawk_engine.config.load_secrets", return_value={}), \
         patch("sys.argv", ["hawk", "--run", "evening", "--consensus"]):
        from tools.hawk import main
        main()

    mock_consensus.assert_called_once()
    mock_single.assert_not_called()


# ---------------------------------------------------------------------------
# (i) CLI --single overrides consensus.enabled=true
# ---------------------------------------------------------------------------

def test_cli_single_overrides_config(tmp_path):
    """--single CLI flag overrides consensus.enabled=true in config."""
    from unittest.mock import patch, MagicMock

    mock_consensus = MagicMock(return_value=0)
    mock_single = MagicMock(return_value=0)

    with patch("tools.hawk.run_evening_consensus", mock_consensus), \
         patch("tools.hawk.run_evening", mock_single), \
         patch("tools.hawk_engine.config.load_hawk_config", return_value={
             "consensus": {"enabled": True, "models": [{"id": "m/1", "name": "M1"}]},
             "output_dir": str(tmp_path),
         }), \
         patch("tools.hawk_engine.config.load_secrets", return_value={}), \
         patch("sys.argv", ["hawk", "--run", "evening", "--single"]):
        from tools.hawk import main
        main()

    mock_single.assert_called_once()
    mock_consensus.assert_not_called()


# ---------------------------------------------------------------------------
# (j) JSON output has correct consensus structure
# ---------------------------------------------------------------------------

def test_consensus_json_has_required_keys():
    """Consensus result contains mode, models_used, consensus_picks, per_model."""
    from tools.hawk_engine.consensus import build_consensus

    pick = _pick("RELIANCE", "LONG", "HIGH", [2850, 2880])
    model_results = [
        _model_result("Claude", [pick], cost=0.02, tokens_in=400,
                      tokens_out=800, elapsed=12.0),
    ]
    result = build_consensus(model_results, total_models=1)

    assert result["mode"] == "consensus"
    assert "models_used" in result
    assert "consensus_picks" in result
    assert "per_model" in result
    assert "aggregate_metadata" in result
    assert result["aggregate_metadata"]["total_cost_usd"] == 0.02
    assert result["aggregate_metadata"]["total_tokens_input"] == 400
    assert result["aggregate_metadata"]["total_tokens_output"] == 800
    assert result["aggregate_metadata"]["total_elapsed_s"] == 12.0

    # Per-model has the right structure
    assert "Claude" in result["per_model"]
    assert result["per_model"]["Claude"]["picks_count"] == 1
    assert result["per_model"]["Claude"]["cost"] == 0.02


# ---------------------------------------------------------------------------
# (k) Telegram consensus format correct
# ---------------------------------------------------------------------------

def test_consensus_telegram_format():
    """Consensus Telegram message contains UNANIMOUS/STRONG sections."""
    from tools.hawk_engine.output_writer import format_consensus_telegram

    result = {
        "date": "2026-03-12",
        "mode": "consensus",
        "models_used": ["Claude", "Gemini", "GPT-5.4"],
        "models_failed": ["Kimi"],
        "total_models": 4,
        "consensus_picks": [
            {
                "rank": 1, "symbol": "HCLTECH", "direction": "SHORT",
                "consensus_tag": "UNANIMOUS", "model_votes": 3,
                "avg_conviction": "HIGH", "entry_zone": [1380, 1395],
                "consensus_score": 9.0,
                "reasoning_summary": "Claude: Broke 20DMA | Gemini: IT weak",
            },
            {
                "rank": 2, "symbol": "SUNPHARMA", "direction": "LONG",
                "consensus_tag": "STRONG", "model_votes": 2,
                "avg_conviction": "MEDIUM", "entry_zone": [1800, 1815],
                "consensus_score": 4.0,
                "reasoning_summary": "Claude: Pharma strength",
            },
        ],
        "metadata": {"total_cost_usd": 0.06, "total_elapsed_s": 45.0},
    }
    msg = format_consensus_telegram(result)

    assert "HAWK Consensus" in msg
    assert "2026-03-12" in msg
    assert "UNANIMOUS" in msg
    assert "STRONG" in msg
    assert "HCLTECH" in msg
    assert "SHORT" in msg
    assert "SUNPHARMA" in msg
    assert "LONG" in msg
    assert "Claude" in msg
    assert "Kimi" in msg
