"""
HAWK — Unit tests for AI Market Intelligence Engine.

Tests:
  (a) data_collector returns correct structure with mock data
  (b) LLM response JSON parsing handles clean JSON and fenced JSON
  (c) output_writer creates correct file path and valid JSON
  (d) Telegram formatter produces expected message format
  (e) dry-run mode skips LLM call
  (f) missing data source handled gracefully (partial data, no crash)
  (g) config loading returns defaults when files missing
  (h) NIFTY 50 hardcoded fallback list has 50 stocks
"""
from __future__ import annotations

import json
import os
import sys

import pytest

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


# ---------------------------------------------------------------------------
# (a) data_collector returns correct structure
# ---------------------------------------------------------------------------

def test_data_collector_returns_correct_structure():
    """collect_evening_data returns all expected top-level keys."""
    from unittest.mock import patch

    # Mock all external calls to return empty/default
    with patch("tools.hawk_engine.data_collector._fetch_nifty50_stocks", return_value=["RELIANCE", "INFY"]):
        with patch("tools.hawk_engine.data_collector._fetch_bhavcopy", return_value=(
            [
                {"symbol": "RELIANCE", "open": 2450, "high": 2480, "low": 2430, "close": 2470, "volume": 1000000, "delivery_pct": 55.0, "change_pct": 0.82},
                {"symbol": "INFY", "open": 1500, "high": 1520, "low": 1490, "close": 1510, "volume": 800000, "delivery_pct": 48.0, "change_pct": 0.67},
            ],
            ["mock"],
        )):
            with patch("tools.hawk_engine.data_collector._fetch_fii_dii", return_value=(
                {"fii_net_equity": -1000.0, "dii_net_equity": 800.0}, ["mock"]
            )):
                with patch("tools.hawk_engine.data_collector._fetch_indices", return_value=(
                    {"nifty_50": {"close": 24000, "change_pct": -0.5}}, ["mock"]
                )):
                    with patch("tools.hawk_engine.data_collector._fetch_sectors", return_value=({}, [])):
                        from datetime import date
                        from tools.hawk_engine.data_collector import collect_evening_data

                        result = collect_evening_data(date(2026, 3, 11))

    assert "date" in result
    assert "bhavcopy" in result
    assert "fii_dii" in result
    assert "indices" in result
    assert "sectors" in result
    assert "top_gainers" in result
    assert "top_losers" in result
    assert "unusual_delivery" in result
    assert "regime" in result
    assert "metadata" in result
    assert result["date"] == "2026-03-11"
    assert len(result["bhavcopy"]) == 2
    assert result["fii_dii"]["fii_net_equity"] == -1000.0


def test_data_collector_derives_movers():
    """Top gainers/losers are correctly derived from bhavcopy."""
    from tools.hawk_engine.data_collector import _derive_movers

    bhav = [
        {"symbol": "A", "change_pct": 5.0},
        {"symbol": "B", "change_pct": -3.0},
        {"symbol": "C", "change_pct": 2.0},
        {"symbol": "D", "change_pct": -1.0},
        {"symbol": "E", "change_pct": 0.5},
    ]
    gainers, losers = _derive_movers(bhav, top_n=2)
    assert len(gainers) == 2
    assert gainers[0]["symbol"] == "A"
    assert gainers[1]["symbol"] == "C"
    assert len(losers) == 2
    assert losers[0]["symbol"] == "B"
    assert losers[1]["symbol"] == "D"


def test_data_collector_derives_unusual_delivery():
    """Unusual delivery detection flags stocks above threshold."""
    from tools.hawk_engine.data_collector import _derive_unusual_delivery

    bhav = [
        {"symbol": "HIGH_DEL", "delivery_pct": 80.0, "close": 100, "change_pct": 1.0},
        {"symbol": "NORMAL", "delivery_pct": 45.0, "close": 200, "change_pct": 0.5},
    ]
    unusual = _derive_unusual_delivery(bhav, threshold=1.5)
    assert len(unusual) == 1
    assert unusual[0]["symbol"] == "HIGH_DEL"


# ---------------------------------------------------------------------------
# (b) LLM response JSON parsing
# ---------------------------------------------------------------------------

def test_llm_parse_clean_json():
    """Parser handles clean JSON array response."""
    from tools.hawk_engine.llm_analyst import _parse_llm_response

    text = '[{"rank": 1, "symbol": "RELIANCE", "direction": "LONG"}]'
    result = _parse_llm_response(text)
    assert len(result) == 1
    assert result[0]["symbol"] == "RELIANCE"


def test_llm_parse_fenced_json():
    """Parser handles JSON inside markdown fences."""
    from tools.hawk_engine.llm_analyst import _parse_llm_response

    text = '```json\n[{"rank": 1, "symbol": "INFY", "direction": "SHORT"}]\n```'
    result = _parse_llm_response(text)
    assert len(result) == 1
    assert result[0]["symbol"] == "INFY"


def test_llm_parse_generic_fenced():
    """Parser handles JSON inside generic code fences (no json tag)."""
    from tools.hawk_engine.llm_analyst import _parse_llm_response

    text = '```\n[{"rank": 1, "symbol": "TCS", "direction": "LONG"}]\n```'
    result = _parse_llm_response(text)
    assert len(result) == 1
    assert result[0]["symbol"] == "TCS"


def test_llm_parse_with_surrounding_text():
    """Parser extracts JSON array even with surrounding commentary."""
    from tools.hawk_engine.llm_analyst import _parse_llm_response

    text = 'Here are my picks:\n[{"rank": 1, "symbol": "HCLTECH"}]\nHope this helps!'
    result = _parse_llm_response(text)
    assert len(result) == 1
    assert result[0]["symbol"] == "HCLTECH"


def test_llm_parse_invalid_raises():
    """Parser raises ValueError on unparseable response."""
    from tools.hawk_engine.llm_analyst import _parse_llm_response

    with pytest.raises(ValueError, match="Could not parse"):
        _parse_llm_response("This is not JSON at all")


# ---------------------------------------------------------------------------
# (c) output_writer creates correct file and valid JSON
# ---------------------------------------------------------------------------

def test_output_writer_creates_json_file(tmp_path):
    """write_json creates the correct file with valid JSON."""
    from tools.hawk_engine.output_writer import write_json

    result = {
        "date": "2026-03-11",
        "run": "evening",
        "regime": "bull_trend",
        "watchlist": [{"rank": 1, "symbol": "RELIANCE"}],
        "metadata": {},
    }
    filepath = write_json(result, str(tmp_path))

    assert os.path.exists(filepath)
    assert filepath.endswith("2026-03-11_evening.json")

    with open(filepath) as f:
        loaded = json.load(f)
    assert loaded["date"] == "2026-03-11"
    assert loaded["run"] == "evening"
    assert len(loaded["watchlist"]) == 1


def test_output_writer_creates_directory(tmp_path):
    """write_json creates output directory if it doesn't exist."""
    from tools.hawk_engine.output_writer import write_json

    nested_dir = str(tmp_path / "deep" / "nested")
    result = {"date": "2026-03-11", "run": "evening", "watchlist": []}
    filepath = write_json(result, nested_dir)
    assert os.path.exists(filepath)


def test_output_writer_load_evening_picks(tmp_path):
    """load_evening_picks reads previously saved evening JSON."""
    from tools.hawk_engine.output_writer import load_evening_picks, write_json

    result = {
        "date": "2026-03-11",
        "run": "evening",
        "watchlist": [{"rank": 1, "symbol": "INFY"}],
    }
    write_json(result, str(tmp_path))
    picks = load_evening_picks("2026-03-11", str(tmp_path))
    assert len(picks) == 1
    assert picks[0]["symbol"] == "INFY"


def test_output_writer_load_missing_returns_empty(tmp_path):
    """load_evening_picks returns empty list for non-existent file."""
    from tools.hawk_engine.output_writer import load_evening_picks

    picks = load_evening_picks("2099-01-01", str(tmp_path))
    assert picks == []


# ---------------------------------------------------------------------------
# (d) Telegram formatter produces expected message format
# ---------------------------------------------------------------------------

def test_telegram_formatter_with_picks():
    """format_telegram_message produces HAWK-Picks format."""
    from tools.hawk_engine.output_writer import format_telegram_message

    result = {
        "date": "2026-03-11",
        "run": "evening",
        "regime": "bear_trend",
        "market_context": {
            "vix": 23.1,
            "fii_net_cr": -2340.5,
        },
        "watchlist": [
            {
                "rank": 1,
                "symbol": "HCLTECH",
                "direction": "SHORT",
                "conviction": "HIGH",
                "entry_zone": [1380, 1395],
                "reasoning": "Broke below 20DMA with 2.1x delivery",
                "risk_flag": None,
            },
            {
                "rank": 2,
                "symbol": "SUNPHARMA",
                "direction": "LONG",
                "conviction": "MEDIUM",
                "entry_zone": [1800, 1815],
                "reasoning": "Pharma relative strength",
                "risk_flag": "Earnings next week",
            },
        ],
    }
    msg = format_telegram_message(result)

    assert "🦅 HAWK Evening" in msg
    assert "2026-03-11" in msg
    assert "bear_trend" in msg
    assert "23.1" in msg
    assert "HCLTECH" in msg
    assert "SHORT" in msg
    assert "HIGH" in msg
    assert "1380-1395" in msg
    assert "🔴" in msg  # SHORT icon
    assert "SUNPHARMA" in msg
    assert "🟢" in msg  # LONG icon
    assert "⚠️ Earnings next week" in msg


def test_telegram_formatter_no_picks():
    """format_telegram_message handles empty watchlist."""
    from tools.hawk_engine.output_writer import format_telegram_message

    result = {
        "date": "2026-03-11",
        "run": "evening",
        "regime": "unknown",
        "market_context": {"vix": "N/A", "fii_net_cr": "N/A"},
        "watchlist": [],
    }
    msg = format_telegram_message(result)
    assert "No picks generated" in msg


# ---------------------------------------------------------------------------
# (e) dry-run mode skips LLM call
# ---------------------------------------------------------------------------

def test_dry_run_skips_llm(tmp_path):
    """In dry-run mode, analyze_evening is never called."""
    from unittest.mock import patch, MagicMock

    with patch("tools.hawk_engine.data_collector.collect_evening_data", return_value={
        "date": "2026-03-11", "bhavcopy": [], "fii_dii": {}, "indices": {},
        "sectors": {}, "top_gainers": [], "top_losers": [], "unusual_delivery": [],
        "regime": "unknown", "metadata": {"data_sources": [], "fallbacks_used": [], "bhavcopy_count": 0},
    }):
        with patch("tools.hawk_engine.output_writer.write_json", return_value=str(tmp_path / "test.json")):
            with patch("tools.hawk_engine.llm_analyst.analyze_evening") as mock_llm:
                from datetime import date
                from tools.hawk import run_evening

                hawk_config = {"output_dir": str(tmp_path)}
                result = run_evening(date(2026, 3, 11), dry_run=True, hawk_config=hawk_config, secrets={})

                mock_llm.assert_not_called()
                assert result == 0


# ---------------------------------------------------------------------------
# (f) missing data source handled gracefully
# ---------------------------------------------------------------------------

def test_missing_data_source_no_crash():
    """All data sources failing should not crash — returns empty data."""
    from unittest.mock import patch
    from datetime import date
    from tools.hawk_engine.data_collector import collect_evening_data

    with patch("tools.hawk_engine.data_collector._fetch_nifty50_stocks", return_value=["RELIANCE"]):
        with patch("tools.hawk_engine.data_collector._fetch_bhavcopy", return_value=([], [])):
            with patch("tools.hawk_engine.data_collector._fetch_fii_dii", return_value=(
                {"fii_net_equity": 0.0, "dii_net_equity": 0.0}, []
            )):
                with patch("tools.hawk_engine.data_collector._fetch_indices", return_value=({}, [])):
                    with patch("tools.hawk_engine.data_collector._fetch_sectors", return_value=({}, [])):
                        result = collect_evening_data(date(2026, 3, 11))

    # Should not crash — returns structure with empty data
    assert result["bhavcopy"] == []
    assert result["top_gainers"] == []
    assert result["top_losers"] == []
    assert result["metadata"]["bhavcopy_count"] == 0


# ---------------------------------------------------------------------------
# (g) config loading defaults
# ---------------------------------------------------------------------------

def test_config_loads_defaults_when_missing():
    """load_hawk_config returns empty dict when file doesn't exist."""
    from unittest.mock import patch

    with patch("builtins.open", side_effect=FileNotFoundError):
        from tools.hawk_engine.config import load_hawk_config
        cfg = load_hawk_config()
    assert cfg == {}


def test_anthropic_api_key_extraction():
    """get_anthropic_api_key extracts from nested secrets."""
    from tools.hawk_engine.config import get_anthropic_api_key

    assert get_anthropic_api_key({"anthropic": {"api_key": "sk-test123"}}) == "sk-test123"
    assert get_anthropic_api_key({}) == ""
    assert get_anthropic_api_key({"anthropic": {}}) == ""


# ---------------------------------------------------------------------------
# (h) NIFTY 50 hardcoded list
# ---------------------------------------------------------------------------

def test_nifty50_hardcoded_list_has_50_stocks():
    """Hardcoded fallback list has exactly 50 NIFTY 50 constituents."""
    from tools.hawk_engine.config import NIFTY_50_STOCKS

    assert len(NIFTY_50_STOCKS) == 50
    assert "RELIANCE" in NIFTY_50_STOCKS
    assert "INFY" in NIFTY_50_STOCKS
    assert "TCS" in NIFTY_50_STOCKS


# ---------------------------------------------------------------------------
# (i) Evening prompt formatting
# ---------------------------------------------------------------------------

def test_evening_prompt_includes_all_sections():
    """get_evening_prompt includes all data sections."""
    from tools.hawk_engine.llm_analyst import get_evening_prompt

    data = {
        "date": "2026-03-11",
        "regime": "bear_trend",
        "indices": {
            "nifty_50": {"close": 24000, "change_pct": -1.2},
            "bank_nifty": {"close": 51000, "change_pct": -1.8},
            "india_vix": {"close": 23.1, "change_pct": 5.0},
        },
        "fii_dii": {"fii_net_equity": -2340.5, "dii_net_equity": 1890.0},
        "sectors": {"nifty_it": {"close": 35000, "change_pct": -2.1}},
        "top_gainers": [{"symbol": "A", "change_pct": 3.0, "volume": 100, "delivery_pct": 50}],
        "top_losers": [{"symbol": "B", "change_pct": -2.0, "volume": 200, "delivery_pct": 45}],
        "unusual_delivery": [{"symbol": "C", "delivery_pct": 80, "ratio": 1.6, "change_pct": 1.0}],
        "bhavcopy": [
            {"symbol": "A", "open": 100, "high": 105, "low": 98, "close": 103, "volume": 100, "delivery_pct": 50, "change_pct": 3.0},
        ],
    }
    prompt = get_evening_prompt(data)

    assert "HAWK Evening Analysis" in prompt
    assert "2026-03-11" in prompt
    assert "Nifty 50:" in prompt
    assert "24000" in prompt
    assert "bear_trend" in prompt
    assert "FII/DII" in prompt
    assert "-2340.5" in prompt
    assert "SECTOR PERFORMANCE" in prompt
    assert "TOP GAINERS" in prompt
    assert "TOP LOSERS" in prompt
    assert "UNUSUAL DELIVERY" in prompt
    assert "FULL BHAVCOPY" in prompt


# ---------------------------------------------------------------------------
# (j) Multi-provider LLM support
# ---------------------------------------------------------------------------

def test_call_llm_routes_to_anthropic():
    """call_llm dispatches to _call_anthropic for provider='anthropic'."""
    from unittest.mock import patch
    from tools.hawk_engine.llm_analyst import call_llm

    mock_response = ('[{"rank": 1, "symbol": "RELIANCE"}]', {"tokens_input": 100, "tokens_output": 50})

    with patch("tools.hawk_engine.llm_analyst._call_anthropic", return_value=mock_response) as mock_fn:
        text, usage = call_llm("anthropic", "sk-test", "model-1", "system", "user", 2000)
        mock_fn.assert_called_once_with("sk-test", "model-1", "system", "user", 2000)
        assert text == mock_response[0]
        assert usage["tokens_input"] == 100


def test_call_llm_routes_to_openrouter():
    """call_llm dispatches to _call_openrouter for provider='openrouter'."""
    from unittest.mock import patch
    from tools.hawk_engine.llm_analyst import call_llm

    mock_response = ('[{"rank": 1, "symbol": "INFY"}]', {"tokens_input": 200, "tokens_output": 80})

    with patch("tools.hawk_engine.llm_analyst._call_openrouter", return_value=mock_response) as mock_fn:
        text, usage = call_llm("openrouter", "or-test", "openai/gpt-4o", "system", "user", 2000, "TestApp")
        mock_fn.assert_called_once_with("or-test", "openai/gpt-4o", "system", "user", 2000, "TestApp")
        assert text == mock_response[0]
        assert usage["tokens_input"] == 200


def test_call_llm_unsupported_provider():
    """call_llm raises ValueError for unknown provider."""
    from tools.hawk_engine.llm_analyst import call_llm

    with pytest.raises(ValueError, match="Unsupported LLM provider"):
        call_llm("gemini", "key", "model", "sys", "user")


def test_anthropic_request_format():
    """_call_anthropic sends correct headers and payload format."""
    from unittest.mock import patch, MagicMock
    from tools.hawk_engine.llm_analyst import _call_anthropic

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "content": [{"type": "text", "text": "[]"}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }

    with patch("tools.hawk_engine.llm_analyst.requests.post", return_value=mock_resp) as mock_post:
        text, usage = _call_anthropic("sk-key", "claude-test", "sys prompt", "user prompt", 1000)

        call_args = mock_post.call_args
        headers = call_args.kwargs.get("headers") or call_args[1].get("headers")
        payload = call_args.kwargs.get("json") or call_args[1].get("json")

        assert headers["x-api-key"] == "sk-key"
        assert headers["anthropic-version"] == "2023-06-01"
        assert payload["model"] == "claude-test"
        assert payload["system"] == "sys prompt"
        assert payload["messages"][0]["role"] == "user"
        assert text == "[]"
        assert usage["tokens_input"] == 10


def test_openrouter_request_format():
    """_call_openrouter sends correct headers and OpenAI-compatible payload."""
    from unittest.mock import patch, MagicMock
    from tools.hawk_engine.llm_analyst import _call_openrouter

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "[]"}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10},
    }

    with patch("tools.hawk_engine.llm_analyst.requests.post", return_value=mock_resp) as mock_post:
        text, usage = _call_openrouter("or-key", "openai/gpt-4o", "sys prompt", "user prompt", 1500, "MyApp")

        call_args = mock_post.call_args
        headers = call_args.kwargs.get("headers") or call_args[1].get("headers")
        payload = call_args.kwargs.get("json") or call_args[1].get("json")

        assert headers["Authorization"] == "Bearer or-key"
        assert headers["X-Title"] == "MyApp"
        assert payload["model"] == "openai/gpt-4o"
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][1]["role"] == "user"
        assert text == "[]"
        assert usage["tokens_input"] == 20
        assert usage["tokens_output"] == 10


def test_analyze_evening_includes_provider_in_metadata():
    """analyze_evening includes provider field in metadata."""
    from unittest.mock import patch
    from tools.hawk_engine.llm_analyst import analyze_evening

    mock_llm = ('[{"rank": 1, "symbol": "TCS", "direction": "LONG"}]', {"tokens_input": 100, "tokens_output": 50})

    with patch("tools.hawk_engine.llm_analyst.call_llm", return_value=mock_llm):
        result = analyze_evening(
            {"date": "2026-03-11", "indices": {}, "fii_dii": {}, "sectors": {},
             "top_gainers": [], "top_losers": [], "unusual_delivery": [], "bhavcopy": []},
            api_key="test-key",
            model="test-model",
            provider="openrouter",
        )
        assert result["metadata"]["provider"] == "openrouter"
        assert result["metadata"]["model"] == "test-model"
        assert len(result["watchlist"]) == 1


# ---------------------------------------------------------------------------
# (k) Config LLM resolution
# ---------------------------------------------------------------------------

def test_get_llm_provider_default():
    """get_llm_provider returns 'anthropic' when not configured."""
    from tools.hawk_engine.config import get_llm_provider

    assert get_llm_provider({}) == "anthropic"


def test_get_llm_provider_configured():
    """get_llm_provider reads from secrets.llm.provider."""
    from tools.hawk_engine.config import get_llm_provider

    assert get_llm_provider({"llm": {"provider": "openrouter"}}) == "openrouter"


def test_get_llm_api_key_new_format():
    """get_llm_api_key reads from new llm.<provider>.api_key format."""
    from tools.hawk_engine.config import get_llm_api_key

    secrets = {"llm": {"provider": "anthropic", "anthropic": {"api_key": "sk-new"}}}
    assert get_llm_api_key(secrets) == "sk-new"

    secrets = {"llm": {"provider": "openrouter", "openrouter": {"api_key": "or-new"}}}
    assert get_llm_api_key(secrets) == "or-new"


def test_get_llm_api_key_backward_compat():
    """get_llm_api_key falls back to old secrets.anthropic.api_key format."""
    from tools.hawk_engine.config import get_llm_api_key

    secrets = {"anthropic": {"api_key": "sk-old"}}
    assert get_llm_api_key(secrets) == "sk-old"


def test_get_llm_api_key_missing():
    """get_llm_api_key returns empty string when no key configured."""
    from tools.hawk_engine.config import get_llm_api_key

    assert get_llm_api_key({}) == ""
    assert get_llm_api_key({"llm": {"provider": "openrouter"}}) == ""


def test_get_anthropic_api_key_backward_compat():
    """get_anthropic_api_key still works with both old and new formats."""
    from tools.hawk_engine.config import get_anthropic_api_key

    # Old format
    assert get_anthropic_api_key({"anthropic": {"api_key": "sk-old"}}) == "sk-old"
    # New format
    assert get_anthropic_api_key({"llm": {"anthropic": {"api_key": "sk-new"}}}) == "sk-new"
    # Empty
    assert get_anthropic_api_key({}) == ""


def test_get_openrouter_site_name():
    """get_openrouter_site_name returns configured or default value."""
    from tools.hawk_engine.config import get_openrouter_site_name

    assert get_openrouter_site_name({}) == "TradeOS-HAWK"
    assert get_openrouter_site_name({"llm": {"openrouter": {"site_name": "MyApp"}}}) == "MyApp"
