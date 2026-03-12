"""
HAWK — Configuration loader.

Loads hawk.yaml + secrets.yaml for HAWK-specific settings.
Standalone — does not depend on main TradeOS process.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent.parent
CONFIG_PATH = ROOT / "config" / "hawk.yaml"
SECRETS_PATH = ROOT / "config" / "secrets.yaml"


def load_hawk_config() -> dict:
    """Load config/hawk.yaml. Returns empty dict on failure."""
    try:
        with open(CONFIG_PATH) as f:
            raw = yaml.safe_load(f) or {}
        return raw.get("hawk", {})
    except FileNotFoundError:
        return {}


def load_secrets() -> dict:
    """Load config/secrets.yaml. Returns empty dict on failure."""
    try:
        with open(SECRETS_PATH) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def get_llm_provider(secrets: dict) -> str:
    """Get configured LLM provider name. Default: anthropic."""
    return str(secrets.get("llm", {}).get("provider", "anthropic")).lower()


def get_llm_api_key(secrets: dict) -> str:
    """Get the API key for the configured LLM provider.

    Supports:
      - New format: secrets.llm.<provider>.api_key
      - Backward compat: secrets.anthropic.api_key (old single-provider format)
    """
    llm = secrets.get("llm", {})
    provider = get_llm_provider(secrets)

    # New format: llm.<provider>.api_key
    provider_cfg = llm.get(provider, {})
    if isinstance(provider_cfg, dict) and provider_cfg.get("api_key"):
        return str(provider_cfg["api_key"])

    # Backward compat: secrets.anthropic.api_key (old format)
    if provider == "anthropic":
        old_key = secrets.get("anthropic", {}).get("api_key", "")
        if old_key:
            return str(old_key)

    return ""


def get_openrouter_site_name(secrets: dict) -> str:
    """Get OpenRouter site name for HTTP-Referer header."""
    return str(secrets.get("llm", {}).get("openrouter", {}).get("site_name", "TradeOS-HAWK"))


def get_anthropic_api_key(secrets: dict) -> str:
    """Extract Anthropic API key from secrets dict.

    Backward compat wrapper — prefers new llm.anthropic.api_key format.
    """
    return get_llm_api_key({**secrets, "llm": {**secrets.get("llm", {}), "provider": "anthropic"}})


def get_hawk_telegram_credentials(secrets: dict) -> tuple[str, str]:
    """Extract HAWK Telegram channel credentials."""
    tg = secrets.get("telegram", {})
    hawk = tg.get("hawk", {})
    if isinstance(hawk, dict):
        return (str(hawk.get("bot_token", "")), str(hawk.get("chat_id", "")))
    return ("", "")


# NIFTY 50 hardcoded fallback list (as of March 2026)
NIFTY_50_STOCKS = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BPCL",
    "BHARTIARTL", "BRITANNIA", "CIPLA", "COALINDIA", "DRREDDY",
    "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH", "HDFCBANK",
    "HDFCLIFE", "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK",
    "ITC", "INDUSINDBK", "INFY", "JSWSTEEL", "KOTAKBANK",
    "LT", "M&M", "MARUTI", "NESTLEIND", "NTPC",
    "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SBIN",
    "SUNPHARMA", "TCS", "TATACONSUM", "TATAMOTORS", "TATASTEEL",
    "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
]
