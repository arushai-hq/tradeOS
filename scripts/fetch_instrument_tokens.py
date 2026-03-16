#!/usr/bin/env python3
"""
Fetch KiteConnect instrument tokens for NIFTY 50 stocks.

Connects to KiteConnect using secrets.yaml credentials, fetches
instruments("NSE"), and outputs symbol→token mapping.

Usage:
    python scripts/fetch_instrument_tokens.py              # Print token map
    python scripts/fetch_instrument_tokens.py --yaml       # Print as YAML for settings.yaml
    python scripts/fetch_instrument_tokens.py --verify     # Verify current settings.yaml tokens

Requires valid KiteConnect session (access_token refreshed today).
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _load_kite():
    """Create authenticated KiteConnect instance from secrets.yaml."""
    from tools.hawk_engine.config import load_secrets
    from kiteconnect import KiteConnect

    secrets = load_secrets()
    api_key = secrets.get("zerodha", {}).get("api_key", "")
    access_token = secrets.get("zerodha", {}).get("access_token", "")

    if not api_key or not access_token:
        print("ERROR: Missing zerodha.api_key or zerodha.access_token in config/secrets.yaml")
        sys.exit(1)

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite


def _get_nifty50_tokens(kite) -> dict[str, int]:
    """Fetch instrument tokens for all NIFTY 50 stocks."""
    from tools.hawk_engine.config import NIFTY_50_STOCKS

    instruments = kite.instruments("NSE")
    token_map = {}

    for inst in instruments:
        if inst["tradingsymbol"] in NIFTY_50_STOCKS and inst.get("instrument_type") == "EQ":
            token_map[inst["tradingsymbol"]] = inst["instrument_token"]

    return token_map


def _print_table(token_map: dict[str, int]) -> None:
    """Print token map as table."""
    from tools.hawk_engine.config import NIFTY_50_STOCKS

    print(f"\n{'Symbol':<16} {'Token':>12}  Status")
    print("-" * 42)
    for symbol in sorted(NIFTY_50_STOCKS):
        token = token_map.get(symbol)
        status = "OK" if token else "NOT FOUND"
        print(f"{symbol:<16} {token or 0:>12}  {status}")

    found = sum(1 for s in NIFTY_50_STOCKS if s in token_map)
    print(f"\n{found}/{len(NIFTY_50_STOCKS)} symbols resolved")


def _print_yaml(token_map: dict[str, int]) -> None:
    """Print as YAML for settings.yaml trading.instruments section."""
    from datetime import datetime
    import pytz

    IST = pytz.timezone("Asia/Kolkata")
    today = datetime.now(IST).strftime("%Y-%m-%d")

    print(f"# NIFTY 50 constituents — Last refreshed: {today}")
    print("trading:")
    print("  instruments:")
    for symbol in sorted(token_map.keys()):
        token = token_map[symbol]
        sym_val = f'"{symbol}"' if "&" in symbol else symbol
        print(f"    - symbol: {sym_val}")
        print(f"      token: {token}")


def _verify_settings(token_map: dict[str, int]) -> None:
    """Verify current settings.yaml tokens against live data."""
    import yaml

    settings_path = os.path.join(ROOT, "config", "settings.yaml")
    with open(settings_path) as f:
        settings = yaml.safe_load(f)

    instruments = settings.get("trading", {}).get("instruments", [])
    watchlist = settings.get("watchlist", [])

    print(f"\nWatchlist count: {len(watchlist)}")
    print(f"Instruments count: {len(instruments)}")

    # Check sync
    instrument_symbols = {i["symbol"] for i in instruments}
    watchlist_set = set(watchlist)

    in_watchlist_not_instruments = watchlist_set - instrument_symbols
    in_instruments_not_watchlist = instrument_symbols - watchlist_set

    if in_watchlist_not_instruments:
        print(f"\nWARN: In watchlist but missing from instruments: {sorted(in_watchlist_not_instruments)}")
    if in_instruments_not_watchlist:
        print(f"\nWARN: In instruments but missing from watchlist: {sorted(in_instruments_not_watchlist)}")
    if not in_watchlist_not_instruments and not in_instruments_not_watchlist:
        print("SYNC: watchlist and instruments are in sync")

    # Verify tokens
    mismatched = []
    for inst in instruments:
        symbol = inst["symbol"]
        yaml_token = inst["token"]
        live_token = token_map.get(symbol)
        if live_token and live_token != yaml_token:
            mismatched.append((symbol, yaml_token, live_token))

    if mismatched:
        print(f"\nTOKEN MISMATCHES ({len(mismatched)}):")
        for symbol, old, new in mismatched:
            print(f"  {symbol}: settings={old} → live={new}")
    else:
        print("TOKENS: All tokens match live data")


def main():
    parser = argparse.ArgumentParser(description="Fetch KiteConnect instrument tokens for NIFTY 50")
    parser.add_argument("--yaml", action="store_true", help="Output as YAML for settings.yaml")
    parser.add_argument("--verify", action="store_true", help="Verify current settings.yaml tokens")
    args = parser.parse_args()

    kite = _load_kite()

    try:
        kite.profile()
        print("KiteConnect: authenticated OK")
    except Exception as e:
        print(f"ERROR: KiteConnect auth failed — {e}")
        print("Ensure access_token is refreshed today via token_server")
        sys.exit(1)

    token_map = _get_nifty50_tokens(kite)

    if args.yaml:
        _print_yaml(token_map)
    elif args.verify:
        _print_table(token_map)
        _verify_settings(token_map)
    else:
        _print_table(token_map)


if __name__ == "__main__":
    main()
