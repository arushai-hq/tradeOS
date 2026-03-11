"""
HAWK — LLM Analyst (Multi-Provider).

Sends structured market data to an LLM and parses a ranked
stock watchlist from the JSON response.

Supported providers:
  - anthropic: Anthropic Messages API (default)
  - openrouter: OpenRouter OpenAI-compatible API

Retry: 1 retry on failure. Timeout: 30 seconds.
"""
from __future__ import annotations

import json
import re
import time

import requests
import structlog

log = structlog.get_logger()

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM_PROMPT_EVENING = (
    "You are HAWK, an expert Indian equity market analyst specializing in NSE "
    "intraday momentum trading. Analyze the provided daily NSE data to identify "
    "high-probability intraday momentum opportunities for the NEXT trading day.\n\n"
    "You must respond with ONLY a valid JSON array — no markdown, no commentary, "
    "no explanation. Each element is an object with these exact fields:\n"
    "  rank (int), symbol (string), direction (\"LONG\" or \"SHORT\"),\n"
    "  conviction (\"HIGH\", \"MEDIUM\", or \"LOW\"),\n"
    "  entry_zone (array of two floats [low, high]),\n"
    "  support (float), resistance (float),\n"
    "  reasoning (string, 1 sentence), risk_flag (string or null)\n\n"
    "Rules:\n"
    "- Rank 1 = highest conviction. Return 10-15 stocks.\n"
    "- LONG only in bull_trend or high_volatility regimes.\n"
    "- SHORT only in bear_trend, high_volatility, or crash regimes.\n"
    "- Entry zone = realistic next-day intraday entry range.\n"
    "- Support/resistance = key levels for stop loss and target.\n"
    "- Reasoning should reference specific data points (delivery %, sector, FII/DII).\n"
    "- risk_flag for earnings, corporate events, or unusual patterns (null if none).\n"
)

SYSTEM_PROMPT_MORNING = (
    "You are HAWK, an expert Indian equity market analyst. You are reviewing your "
    "evening picks in light of overnight global developments.\n\n"
    "You must respond with ONLY a valid JSON array — same format as evening picks:\n"
    "  rank, symbol, direction, conviction, entry_zone, support, resistance, "
    "reasoning, risk_flag\n\n"
    "Rules:\n"
    "- Remove picks invalidated by overnight data.\n"
    "- Adjust conviction levels if global data supports or contradicts.\n"
    "- You may add 1-2 new picks if overnight data reveals opportunities.\n"
    "- Flag any regime contradictions in risk_flag.\n"
)


def _format_evening_prompt(data: dict) -> str:
    """Format collected market data into a user prompt for the LLM."""
    lines = [f"=== HAWK Evening Analysis — {data['date']} ===\n"]

    # Market context
    indices = data.get("indices", {})
    nifty = indices.get("nifty_50", {})
    bnifty = indices.get("bank_nifty", {})
    vix = indices.get("india_vix", {})
    lines.append("MARKET OVERVIEW:")
    lines.append(f"  Nifty 50: {nifty.get('close', 'N/A')} ({nifty.get('change_pct', 'N/A')}%)")
    lines.append(f"  Bank Nifty: {bnifty.get('close', 'N/A')} ({bnifty.get('change_pct', 'N/A')}%)")
    lines.append(f"  India VIX: {vix.get('close', 'N/A')} ({vix.get('change_pct', 'N/A')}%)")
    lines.append(f"  Regime: {data.get('regime', 'unknown')}")
    lines.append("")

    # FII/DII
    fii = data.get("fii_dii", {})
    lines.append("FII/DII FLOWS (Cr):")
    lines.append(f"  FII Net Equity: {fii.get('fii_net_equity', 'N/A')}")
    lines.append(f"  DII Net Equity: {fii.get('dii_net_equity', 'N/A')}")
    lines.append("")

    # Sectors
    sectors = data.get("sectors", {})
    if sectors:
        lines.append("SECTOR PERFORMANCE:")
        for name, vals in sorted(sectors.items(), key=lambda x: x[1].get("change_pct", 0)):
            lines.append(f"  {name}: {vals.get('change_pct', 'N/A')}%")
        lines.append("")

    # Top movers
    gainers = data.get("top_gainers", [])
    losers = data.get("top_losers", [])
    if gainers:
        lines.append("TOP GAINERS:")
        for g in gainers[:5]:
            lines.append(f"  {g['symbol']}: {g.get('change_pct', 0):+.2f}% | Vol: {g.get('volume', 0):,} | Del: {g.get('delivery_pct', 0):.1f}%")
        lines.append("")
    if losers:
        lines.append("TOP LOSERS:")
        for l in losers[:5]:
            lines.append(f"  {l['symbol']}: {l.get('change_pct', 0):+.2f}% | Vol: {l.get('volume', 0):,} | Del: {l.get('delivery_pct', 0):.1f}%")
        lines.append("")

    # Unusual delivery
    unusual = data.get("unusual_delivery", [])
    if unusual:
        lines.append("UNUSUAL DELIVERY (>1.5x average):")
        for u in unusual[:10]:
            lines.append(f"  {u['symbol']}: {u.get('delivery_pct', 0):.1f}% ({u.get('ratio', 0):.1f}x avg) | {u.get('change_pct', 0):+.2f}%")
        lines.append("")

    # Full bhavcopy summary
    bhav = data.get("bhavcopy", [])
    if bhav:
        lines.append(f"FULL BHAVCOPY ({len(bhav)} stocks):")
        for s in sorted(bhav, key=lambda x: x.get("change_pct", 0)):
            lines.append(
                f"  {s['symbol']:<12} O:{s.get('open', 0):>8.1f} H:{s.get('high', 0):>8.1f} "
                f"L:{s.get('low', 0):>8.1f} C:{s.get('close', 0):>8.1f} "
                f"Chg:{s.get('change_pct', 0):>+6.2f}% Vol:{s.get('volume', 0):>12,} "
                f"Del:{s.get('delivery_pct', 0):>5.1f}%"
            )

    return "\n".join(lines)


def _format_morning_prompt(evening_picks: list[dict], overnight_data: dict | None) -> str:
    """Format morning update prompt."""
    lines = ["=== HAWK Morning Update ===\n"]
    lines.append("EVENING PICKS TO REVIEW:")
    for pick in evening_picks:
        lines.append(
            f"  {pick.get('rank', '?')}. {pick.get('symbol', '?')} "
            f"{pick.get('direction', '?')} [{pick.get('conviction', '?')}] "
            f"Entry: {pick.get('entry_zone', [0, 0])}"
        )
    lines.append("")

    if overnight_data:
        lines.append("OVERNIGHT GLOBAL DATA:")
        for key, val in overnight_data.items():
            lines.append(f"  {key}: {val}")
    else:
        lines.append("OVERNIGHT DATA: Not available. Review picks based on evening analysis only.")

    return "\n".join(lines)


def _parse_llm_response(text: str) -> list[dict]:
    """
    Parse LLM response into a list of pick dicts.

    Handles:
    - Clean JSON array
    - JSON inside markdown fences (```json ... ```)
    - JSON inside generic fences (``` ... ```)
    """
    # Try direct parse first
    stripped = text.strip()
    try:
        result = json.loads(stripped)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown fences
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)```", stripped, re.DOTALL)
    if fence_match:
        try:
            result = json.loads(fence_match.group(1).strip())
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    # Try finding array brackets
    bracket_match = re.search(r"\[.*\]", stripped, re.DOTALL)
    if bracket_match:
        try:
            result = json.loads(bracket_match.group(0))
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not parse LLM response as JSON array: {text[:200]}")


def _call_anthropic(
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 2000,
) -> tuple[str, dict]:
    """
    Call Anthropic Messages API.

    Returns:
        (response_text, usage_metadata)
    """
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }

    resp = requests.post(
        ANTHROPIC_API_URL,
        headers=headers,
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            text += block.get("text", "")

    usage = data.get("usage", {})
    metadata = {
        "tokens_input": usage.get("input_tokens", 0),
        "tokens_output": usage.get("output_tokens", 0),
    }

    return text, metadata


def _call_openrouter(
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 2000,
    site_name: str = "TradeOS-HAWK",
) -> tuple[str, dict]:
    """
    Call OpenRouter OpenAI-compatible API.

    Returns:
        (response_text, usage_metadata)
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": f"https://github.com/arushai-tech/tradeos",
        "X-Title": site_name,
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    resp = requests.post(
        OPENROUTER_API_URL,
        headers=headers,
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    text = ""
    choices = data.get("choices", [])
    if choices:
        text = choices[0].get("message", {}).get("content", "")

    usage = data.get("usage", {})
    metadata = {
        "tokens_input": usage.get("prompt_tokens", 0),
        "tokens_output": usage.get("completion_tokens", 0),
    }

    return text, metadata


def call_llm(
    provider: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 2000,
    site_name: str = "TradeOS-HAWK",
) -> tuple[str, dict]:
    """
    Unified LLM call dispatcher.

    Routes to the correct provider backend based on provider string.

    Args:
        provider:      "anthropic" or "openrouter"
        api_key:       Provider API key
        model:         Model ID (provider-specific)
        system_prompt: System instructions
        user_prompt:   User message
        max_tokens:    Max response tokens
        site_name:     App name for OpenRouter headers

    Returns:
        (response_text, usage_metadata)

    Raises:
        ValueError: If provider is not supported
    """
    if provider == "anthropic":
        return _call_anthropic(api_key, model, system_prompt, user_prompt, max_tokens)
    elif provider == "openrouter":
        return _call_openrouter(api_key, model, system_prompt, user_prompt, max_tokens, site_name)
    else:
        raise ValueError(f"Unsupported LLM provider: {provider!r}. Use 'anthropic' or 'openrouter'.")


def get_evening_prompt(data: dict) -> str:
    """Public accessor for the formatted evening prompt (used by dry-run)."""
    return _format_evening_prompt(data)


def analyze_evening(
    data: dict,
    api_key: str,
    model: str = "claude-sonnet-4-20250514",
    max_tokens: int = 2000,
    watchlist_size: int = 15,
    provider: str = "anthropic",
    site_name: str = "TradeOS-HAWK",
) -> dict:
    """
    Send evening data to LLM, get watchlist back.

    Retries once on failure. Returns structured result dict.

    Args:
        data:           Collected market data from data_collector.
        api_key:        Provider API key.
        model:          Model ID (provider-specific).
        max_tokens:     Max response tokens.
        watchlist_size: Expected number of picks.
        provider:       LLM provider ("anthropic" or "openrouter").
        site_name:      App name for OpenRouter headers.

    Returns:
        Dict with watchlist, metadata, and data context.
    """
    user_prompt = _format_evening_prompt(data)

    system = SYSTEM_PROMPT_EVENING + f"\nReturn exactly {watchlist_size} stocks."

    last_error = None
    for attempt in range(2):  # 1 retry
        try:
            start = time.monotonic()
            response_text, usage = call_llm(
                provider, api_key, model, system, user_prompt, max_tokens, site_name
            )
            elapsed = time.monotonic() - start

            watchlist = _parse_llm_response(response_text)

            # Estimate cost (Sonnet 4 pricing: $3/M input, $15/M output)
            cost_usd = (
                usage["tokens_input"] * 3.0 / 1_000_000
                + usage["tokens_output"] * 15.0 / 1_000_000
            )

            log.info(
                "hawk_llm_analysis_complete",
                provider=provider,
                model=model,
                picks=len(watchlist),
                tokens_in=usage["tokens_input"],
                tokens_out=usage["tokens_output"],
                cost_usd=round(cost_usd, 4),
                elapsed_s=round(elapsed, 2),
                attempt=attempt + 1,
            )

            return {
                "watchlist": watchlist,
                "metadata": {
                    "provider": provider,
                    "model": model,
                    "tokens_input": usage["tokens_input"],
                    "tokens_output": usage["tokens_output"],
                    "cost_usd": round(cost_usd, 4),
                    "elapsed_s": round(elapsed, 2),
                },
            }

        except Exception as exc:
            last_error = exc
            log.warning(
                "hawk_llm_attempt_failed",
                provider=provider,
                attempt=attempt + 1,
                error=str(exc),
            )
            if attempt == 0:
                time.sleep(2)  # Brief pause before retry

    log.error("hawk_llm_analysis_failed", provider=provider, error=str(last_error))
    return {
        "watchlist": [],
        "metadata": {"provider": provider, "model": model, "error": str(last_error)},
    }


def analyze_morning(
    evening_picks: list[dict],
    overnight_data: dict | None = None,
    api_key: str = "",
    model: str = "claude-sonnet-4-20250514",
    max_tokens: int = 2000,
    provider: str = "anthropic",
    site_name: str = "TradeOS-HAWK",
) -> dict:
    """
    Morning update — review evening picks with overnight data.

    MVP stub: returns evening picks unchanged if no API key or no overnight data.
    """
    if not api_key:
        log.info("hawk_morning_stub", note="No API key — returning evening picks unchanged")
        return {"watchlist": evening_picks, "metadata": {"provider": provider, "model": model, "note": "stub"}}

    user_prompt = _format_morning_prompt(evening_picks, overnight_data)

    try:
        response_text, usage = call_llm(
            provider, api_key, model, SYSTEM_PROMPT_MORNING, user_prompt, max_tokens, site_name
        )
        watchlist = _parse_llm_response(response_text)
        cost_usd = (
            usage["tokens_input"] * 3.0 / 1_000_000
            + usage["tokens_output"] * 15.0 / 1_000_000
        )
        return {
            "watchlist": watchlist,
            "metadata": {
                "provider": provider,
                "model": model,
                "tokens_input": usage["tokens_input"],
                "tokens_output": usage["tokens_output"],
                "cost_usd": round(cost_usd, 4),
            },
        }
    except Exception as exc:
        log.error("hawk_morning_analysis_failed", provider=provider, error=str(exc))
        return {"watchlist": evening_picks, "metadata": {"provider": provider, "model": model, "error": str(exc)}}
