#!/usr/bin/env python3
"""
TradeOS — Historical Data Downloader

Downloads OHLCV candle data from KiteConnect for backtesting.
Supports multiple intervals with resume capability and rate limiting.

Usage:
    python tools/data_downloader.py download --interval 15min --days 1095
    python tools/data_downloader.py download --all
    python tools/data_downloader.py download --symbol RELIANCE --interval 15min --days 200
    python tools/data_downloader.py status
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import date, datetime, timedelta

# Add project root to path so imports work standalone
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytz
import yaml

from utils.progress import spinner, step_done, step_fail, step_info

IST = pytz.timezone("Asia/Kolkata")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# CLI/DB interval name → KiteConnect API interval name
INTERVAL_MAP: dict[str, str] = {
    "5min": "5minute",
    "15min": "15minute",
    "30min": "30minute",
    "1hour": "60minute",
    "day": "day",
}

# Max days per KiteConnect API call per interval
INTERVAL_MAX_DAYS: dict[str, int] = {
    "5min": 100,
    "15min": 200,
    "30min": 200,
    "1hour": 400,
    "day": 2000,
}

# Default download depth for --all flag
DEFAULT_DAYS: dict[str, int] = {
    "5min": 365,
    "15min": 1095,
    "30min": 1095,
    "1hour": 1095,
    "day": 2555,
}

# NSE index instruments (tokens are stable)
INDEX_INSTRUMENTS: list[dict[str, object]] = [
    {"symbol": "NIFTY 50", "token": 256265},
    {"symbol": "INDIA VIX", "token": 264969},
]

# Rate limit safety margin — Zerodha allows ~3 req/sec
RATE_LIMIT_SECS: float = 0.35


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _get_nested(d: dict, dotted_key: str) -> object:
    """Traverse a nested dict by dot-separated key path."""
    val: object = d
    for part in dotted_key.split("."):
        if not isinstance(val, dict):
            return None
        val = val.get(part)
    return val


def _load_config() -> dict:
    """Load settings.yaml."""
    settings_path = os.path.join(ROOT, "config", "settings.yaml")
    with open(settings_path) as f:
        return yaml.safe_load(f) or {}


def _load_secrets() -> dict:
    """Load secrets.yaml. Returns empty dict on failure."""
    secrets_path = os.path.join(ROOT, "config", "secrets.yaml")
    try:
        with open(secrets_path) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _load_dsn() -> str:
    """Load DB DSN from config files, matching main.py pattern."""
    config = _load_config()
    secrets = _load_secrets()
    return str(
        _get_nested(config, "database.dsn")
        or _get_nested(config, "db.dsn")
        or _get_nested(secrets, "database.dsn")
        or ""
    )


def _load_instruments(config: dict) -> list[dict]:
    """Load instrument list from settings.yaml trading.instruments."""
    instruments = config.get("trading", {}).get("instruments", [])
    return [{"symbol": i["symbol"], "token": i["token"]} for i in instruments]


def _init_kite():
    """Create and verify KiteConnect instance from secrets."""
    from kiteconnect import KiteConnect

    secrets = _load_secrets()
    api_key = secrets.get("zerodha", {}).get("api_key", "")
    access_token = secrets.get("zerodha", {}).get("access_token", "")

    if not api_key or not access_token:
        print("ERROR: Missing Zerodha credentials in config/secrets.yaml")
        print("Run: tradeos auth")
        sys.exit(1)

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    try:
        kite.profile()
    except Exception as exc:
        print(f"ERROR: KiteConnect auth failed — {exc}")
        print("Run: tradeos auth")
        sys.exit(1)

    return kite


# ---------------------------------------------------------------------------
# Date chunking
# ---------------------------------------------------------------------------

def _chunk_date_range(
    start_date: date, end_date: date, interval: str
) -> list[tuple[date, date]]:
    """Split date range into chunks respecting per-interval API limits.

    Returns list of (chunk_start, chunk_end) tuples.
    """
    if start_date > end_date:
        return []

    max_days = INTERVAL_MAX_DAYS[interval]
    chunks: list[tuple[date, date]] = []
    cursor = start_date

    while cursor <= end_date:
        chunk_end = min(cursor + timedelta(days=max_days - 1), end_date)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)

    return chunks


# ---------------------------------------------------------------------------
# KiteConnect download (sync)
# ---------------------------------------------------------------------------

def _download_symbol(
    kite,
    token: int,
    symbol: str,
    interval: str,
    start_date: date,
    end_date: date,
) -> list[dict]:
    """Download historical candles for one symbol via KiteConnect.

    Handles chunking and rate limiting. Returns list of candle dicts.
    """
    kite_interval = INTERVAL_MAP[interval]
    chunks = _chunk_date_range(start_date, end_date, interval)
    all_candles: list[dict] = []

    for i, (chunk_start, chunk_end) in enumerate(chunks):
        from_dt = datetime(chunk_start.year, chunk_start.month, chunk_start.day, 0, 0, 0)
        to_dt = datetime(chunk_end.year, chunk_end.month, chunk_end.day, 23, 59, 59)

        candles = kite.historical_data(token, from_dt, to_dt, kite_interval, False)
        all_candles.extend(candles)

        # Rate limit between API calls
        if i < len(chunks) - 1:
            time.sleep(RATE_LIMIT_SECS)

    return all_candles


# ---------------------------------------------------------------------------
# DB operations (async)
# ---------------------------------------------------------------------------

async def _get_metadata(pool, token: int, interval: str) -> dict | None:
    """Check backtest_metadata for existing download info."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT date_from, date_to, rows_downloaded "
            "FROM backtest_metadata "
            "WHERE instrument_token = $1 AND interval = $2",
            token, interval,
        )
        if row:
            return dict(row)
    return None


async def _insert_candles(
    pool,
    token: int,
    symbol: str,
    interval: str,
    candles: list[dict],
) -> int:
    """Batch insert candles with ON CONFLICT DO NOTHING. Returns count inserted."""
    if not candles:
        return 0

    sql = (
        "INSERT INTO backtest_candles "
        "(instrument_token, symbol, interval, open, high, low, close, volume, "
        "oi, candle_time, session_date) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) "
        "ON CONFLICT DO NOTHING"
    )

    rows = []
    for c in candles:
        candle_dt = c["date"]
        # KiteConnect returns naive datetimes in IST — localize
        if candle_dt.tzinfo is None:
            candle_dt = IST.localize(candle_dt)
        session_dt = candle_dt.date()
        rows.append((
            token, symbol, interval,
            float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]),
            int(c["volume"]),
            int(c.get("oi", 0)) if c.get("oi") else None,
            candle_dt, session_dt,
        ))

    async with pool.acquire() as conn:
        await conn.executemany(sql, rows)

    return len(rows)


async def _update_metadata(
    pool,
    token: int,
    symbol: str,
    interval: str,
    date_from: date,
    date_to: date,
    rows_downloaded: int,
) -> None:
    """Upsert download metadata."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO backtest_metadata "
            "(symbol, instrument_token, interval, date_from, date_to, "
            "rows_downloaded, downloaded_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, NOW()) "
            "ON CONFLICT ON CONSTRAINT uq_bt_metadata_symbol_interval "
            "DO UPDATE SET date_from = LEAST(backtest_metadata.date_from, $4), "
            "date_to = GREATEST(backtest_metadata.date_to, $5), "
            "rows_downloaded = $6, downloaded_at = NOW()",
            symbol, token, interval, date_from, date_to, rows_downloaded,
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def _download_and_store(
    kite,
    pool,
    instruments: list[dict],
    interval: str,
    days: int,
) -> dict:
    """Download one interval for all instruments. Returns summary."""
    end_date = date.today()
    start_date = end_date - timedelta(days=days)
    total = len(instruments)
    downloaded = 0
    skipped = 0
    total_candles = 0
    failed: list[dict] = []

    for idx, inst in enumerate(instruments, 1):
        symbol = inst["symbol"]
        token = inst["token"]

        try:
            # Check resume point
            meta = await _get_metadata(pool, token, interval)
            if meta and meta["date_from"] <= start_date and meta["date_to"] >= end_date:
                step_info(f"[{idx}/{total}] {symbol} {interval} — already complete, skipping")
                skipped += 1
                continue

            # Determine actual start (resume from last downloaded)
            actual_start = start_date
            if meta and meta["date_to"]:
                resume_from = meta["date_to"] + timedelta(days=1)
                if resume_from > actual_start:
                    actual_start = resume_from

            if actual_start > end_date:
                step_info(f"[{idx}/{total}] {symbol} {interval} — up to date")
                skipped += 1
                continue

            # Download
            with spinner(f"[{idx}/{total}] {symbol} — downloading {interval} ({days} days)..."):
                candles = _download_symbol(kite, token, symbol, interval, actual_start, end_date)

            if candles:
                count = await _insert_candles(pool, token, symbol, interval, candles)
                first_dt = candles[0]["date"]
                last_dt = candles[-1]["date"]
                first_date = first_dt.date() if hasattr(first_dt, "date") else first_dt
                last_date = last_dt.date() if hasattr(last_dt, "date") else last_dt
                await _update_metadata(pool, token, symbol, interval, first_date, last_date, count)
                total_candles += count
                step_done(f"{symbol} — {count:,} candles ({first_date} → {last_date})")
            else:
                step_info(f"{symbol} — no candles returned")

            downloaded += 1

        except Exception as exc:
            step_fail(f"{symbol} — {type(exc).__name__}: {exc}")
            failed.append({"symbol": symbol, "error": str(exc)})

    return {
        "interval": interval,
        "downloaded": downloaded,
        "skipped": skipped,
        "total_candles": total_candles,
        "failed": failed,
    }


async def _show_status(pool) -> None:
    """Query and display download coverage per interval."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT interval, COUNT(DISTINCT symbol) as symbols, "
            "SUM(rows_downloaded) as total_candles, "
            "MIN(date_from) as earliest, MAX(date_to) as latest, "
            "MAX(downloaded_at) as last_download "
            "FROM backtest_metadata "
            "GROUP BY interval "
            "ORDER BY interval"
        )

    if not rows:
        print("\nBacktest Data Status")
        print("=" * 50)
        for iv in INTERVAL_MAP:
            print(f"  {iv:8s}  Not downloaded yet")
        print()
        return

    downloaded_intervals = {r["interval"] for r in rows}

    print("\nBacktest Data Status")
    print("=" * 80)
    print(f"  {'Interval':<10s} {'Symbols':>8s} {'Candles':>12s} {'From':>12s} {'To':>12s} {'Last Download':>20s}")
    print("  " + "-" * 76)

    for iv in INTERVAL_MAP:
        if iv in downloaded_intervals:
            row = next(r for r in rows if r["interval"] == iv)
            candles = row["total_candles"] or 0
            last_dl = row["last_download"].strftime("%Y-%m-%d %H:%M") if row["last_download"] else "—"
            print(
                f"  {iv:<10s} {row['symbols']:>8d} {candles:>12,d} "
                f"{str(row['earliest']):>12s} {str(row['latest']):>12s} {last_dl:>20s}"
            )
        else:
            print(f"  {iv:<10s}     Not downloaded yet")

    # Grand total
    total_symbols = set()
    grand_candles = 0
    async with pool.acquire() as conn:
        detail_rows = await conn.fetch(
            "SELECT DISTINCT symbol FROM backtest_metadata"
        )
        grand_candles_row = await conn.fetchval(
            "SELECT SUM(rows_downloaded) FROM backtest_metadata"
        )
    total_symbols = len(detail_rows)
    grand_candles = grand_candles_row or 0

    print("  " + "-" * 76)
    print(f"  Total: {total_symbols} symbols, {len(downloaded_intervals)} intervals, {grand_candles:,} candles")
    print()


# ---------------------------------------------------------------------------
# Async entry points
# ---------------------------------------------------------------------------

async def _run_download(args) -> int:
    """Async entry: resolve instruments, create pool, download."""
    import asyncpg

    config = _load_config()
    dsn = _load_dsn()
    if not dsn:
        print("ERROR: No database DSN found in config/settings.yaml")
        return 1

    # Resolve instruments
    instruments = _load_instruments(config)
    if not instruments:
        print("ERROR: No instruments found in config/settings.yaml trading.instruments")
        return 1

    if args.symbol:
        # Single symbol mode
        match = [i for i in instruments if i["symbol"].upper() == args.symbol.upper()]
        if not match:
            print(f"ERROR: Symbol '{args.symbol}' not found in settings.yaml instruments")
            return 1
        instruments = match

    # Auth check
    with spinner("Verifying KiteConnect auth..."):
        kite = _init_kite()
    step_done("KiteConnect authenticated")

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=5)

    try:
        if args.all:
            # Download all intervals with recommended durations
            all_instruments = instruments + INDEX_INSTRUMENTS
            order = ["day", "1hour", "30min", "15min", "5min"]
            grand_total = 0
            grand_failed: list[dict] = []

            for i, interval in enumerate(order, 1):
                days = DEFAULT_DAYS[interval]
                print(f"\n{'='*60}")
                print(f"Interval {i}/{len(order)}: {interval} ({days} days)")
                print(f"{'='*60}")
                result = await _download_and_store(kite, pool, all_instruments, interval, days)
                grand_total += result["total_candles"]
                grand_failed.extend(result["failed"])

            print(f"\n{'='*60}")
            step_done(f"All intervals complete — {grand_total:,} total candles")
            if grand_failed:
                step_fail(f"{len(grand_failed)} failures: {', '.join(f['symbol'] for f in grand_failed)}")
        else:
            # Single interval mode
            interval = args.interval
            days = args.days
            target_instruments = instruments
            if args.stocks == "nifty50":
                target_instruments = instruments + INDEX_INSTRUMENTS

            print(f"\nDownloading {interval} candles ({days} days) for {len(target_instruments)} instruments...")
            result = await _download_and_store(kite, pool, target_instruments, interval, days)

            print()
            step_done(
                f"{interval} complete — {result['downloaded']} downloaded, "
                f"{result['skipped']} skipped, {result['total_candles']:,} candles"
            )
            if result["failed"]:
                step_fail(
                    f"{len(result['failed'])} failures: "
                    f"{', '.join(f['symbol'] for f in result['failed'])}"
                )
    finally:
        await pool.close()

    return 0


async def _run_status(args) -> int:
    """Async entry: show download coverage."""
    import asyncpg

    dsn = _load_dsn()
    if not dsn:
        print("ERROR: No database DSN found in config/settings.yaml")
        return 1

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        await _show_status(pool)
    finally:
        await pool.close()

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="data_downloader",
        description="TradeOS Historical Data Downloader",
    )
    sub = parser.add_subparsers(dest="command")

    # download subcommand
    dl = sub.add_parser("download", help="Download historical candles from KiteConnect")
    dl.add_argument(
        "--interval",
        choices=list(INTERVAL_MAP.keys()),
        default="15min",
        help="Candle interval (default: 15min)",
    )
    dl.add_argument(
        "--days",
        type=int,
        default=1095,
        help="Number of days to download (default: 1095)",
    )
    dl.add_argument(
        "--stocks",
        choices=["nifty50"],
        default="nifty50",
        help="Stock set to download (default: nifty50)",
    )
    dl.add_argument(
        "--symbol",
        type=str,
        help="Download a single symbol (overrides --stocks)",
    )
    dl.add_argument(
        "--all",
        action="store_true",
        help="Download ALL 5 intervals with recommended durations",
    )

    # status subcommand
    sub.add_parser("status", help="Show download coverage per interval")

    args = parser.parse_args()

    if args.command == "download":
        return asyncio.run(_run_download(args))
    elif args.command == "status":
        return asyncio.run(_run_status(args))
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
