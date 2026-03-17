#!/usr/bin/env python3
"""
TradeOS — Futures Historical Data Downloader

Downloads OHLCV+OI candle data from KiteConnect for NIFTY/BANKNIFTY futures.
Uses continuous=True to stitch expired contracts automatically.
Supports resume capability, rate limiting, and idempotent inserts.

Usage:
    python tools/futures_data_downloader.py download --interval 15min --days 548
    python tools/futures_data_downloader.py download --all
    python tools/futures_data_downloader.py download --instrument NIFTY
    python tools/futures_data_downloader.py status
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
    "day": "day",
}

# Max days per KiteConnect API call per interval
INTERVAL_MAX_DAYS: dict[str, int] = {
    "5min": 100,
    "15min": 200,
    "day": 2000,
}

# Default download depth for --all flag (18 months ≈ 548 days)
DEFAULT_DAYS: dict[str, int] = {
    "5min": 548,
    "15min": 548,
    "day": 548,
}

# Rate limit safety margin — Zerodha allows ~3 req/sec
RATE_LIMIT_SECS: float = 0.35

# Self-healing DDL — embedded so downloader is standalone
_FUTURES_TABLES_SQL = """\
CREATE TABLE IF NOT EXISTS backtest_futures_candles (
    instrument         TEXT           NOT NULL,
    tradingsymbol      TEXT           NOT NULL DEFAULT '',
    expiry             DATE,
    interval           TEXT           NOT NULL,
    timestamp          TIMESTAMPTZ    NOT NULL,
    open               NUMERIC(12,2)  NOT NULL,
    high               NUMERIC(12,2)  NOT NULL,
    low                NUMERIC(12,2)  NOT NULL,
    close              NUMERIC(12,2)  NOT NULL,
    volume             BIGINT         NOT NULL,
    oi                 BIGINT,

    PRIMARY KEY (instrument, tradingsymbol, interval, timestamp),

    CONSTRAINT chk_fut_instrument CHECK (
        instrument IN ('NIFTY', 'BANKNIFTY')
    ),
    CONSTRAINT chk_fut_interval CHECK (
        interval IN ('5min', '15min', 'day')
    )
);

CREATE TABLE IF NOT EXISTS backtest_futures_metadata (
    instrument         TEXT           NOT NULL,
    interval           TEXT           NOT NULL,
    first_candle       TIMESTAMPTZ,
    last_candle        TIMESTAMPTZ,
    candle_count       INTEGER        DEFAULT 0,
    lot_size           INTEGER,
    last_download      TIMESTAMPTZ    DEFAULT NOW(),

    PRIMARY KEY (instrument, interval),

    CONSTRAINT chk_futm_instrument CHECK (
        instrument IN ('NIFTY', 'BANKNIFTY')
    )
);
"""


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


def _load_futures_config(config: dict) -> list[dict]:
    """Load futures instrument config from settings.yaml.

    Returns list of dicts: [{name, lot_size, exclude_prefixes}]
    Falls back to defaults if config section missing.
    """
    futures_cfg = config.get("futures", {})
    instruments_cfg = futures_cfg.get("instruments", [])

    if not instruments_cfg:
        # Defaults when config section missing
        return [
            {"name": "NIFTY", "lot_size": 65, "exclude_prefixes": [
                "NIFTYIT", "NIFTYNXT50", "FINNIFTY", "MIDCPNIFTY",
            ]},
            {"name": "BANKNIFTY", "lot_size": 30, "exclude_prefixes": []},
        ]

    return [
        {
            "name": i["name"],
            "lot_size": i.get("lot_size", 0),
            "exclude_prefixes": i.get("exclude_prefixes", []),
        }
        for i in instruments_cfg
    ]


# ---------------------------------------------------------------------------
# Instrument resolution
# ---------------------------------------------------------------------------

def _resolve_futures_contracts(kite, config: dict) -> list[dict]:
    """Resolve ALL active futures contracts from NFO instrument dump.

    Returns list of {name, token, lot_size, expiry, tradingsymbol}
    for ALL contracts where expiry >= today. Sorted by (name, expiry).

    Daily downloads use only the nearest contract (via _pick_nearest_per_instrument).
    Intraday downloads use all contracts (each downloaded separately).
    """
    futures_instruments = _load_futures_config(config)
    target_names = {i["name"] for i in futures_instruments}

    # Build set of all excluded names
    all_excluded: set[str] = set()
    for i in futures_instruments:
        all_excluded.update(i.get("exclude_prefixes", []))

    # Build config lot sizes as fallback
    config_lot_sizes = {i["name"]: i["lot_size"] for i in futures_instruments}

    # Fetch full NFO instrument dump
    nfo_instruments = kite.instruments("NFO")

    # Filter to NFO-FUT segment, target names only, exclude unwanted
    candidates: list[dict] = []
    for inst in nfo_instruments:
        if inst.get("segment") != "NFO-FUT":
            continue
        name = inst.get("name", "")
        if name not in target_names:
            continue
        if name in all_excluded:
            continue
        candidates.append(inst)

    # Return ALL active contracts (expiry >= today), sorted by (name, expiry)
    today = date.today()
    resolved: list[dict] = []

    for c in candidates:
        if c["expiry"] < today:
            continue
        resolved.append({
            "name": c["name"],
            "token": c["instrument_token"],
            "lot_size": c.get("lot_size", config_lot_sizes.get(c["name"], 0)),
            "expiry": c["expiry"],
            "tradingsymbol": c.get("tradingsymbol", ""),
        })

    resolved.sort(key=lambda r: (r["name"], r["expiry"]))

    # Warn if any configured instrument has no contracts
    resolved_names = {r["name"] for r in resolved}
    for name in target_names:
        if name not in resolved_names:
            step_fail(f"No active futures contract found for {name}")

    return resolved


def _pick_nearest_per_instrument(contracts: list[dict]) -> list[dict]:
    """Filter contracts to only the nearest expiry per instrument name.

    Used for daily continuous downloads where we need a single token per instrument.
    """
    nearest: dict[str, dict] = {}
    for c in contracts:
        name = c["name"]
        if name not in nearest or c["expiry"] < nearest[name]["expiry"]:
            nearest[name] = c
    return list(nearest.values())


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

def _download_futures(
    kite,
    token: int,
    instrument_name: str,
    interval: str,
    start_date: date,
    end_date: date,
    *,
    continuous: bool = False,
) -> list[dict]:
    """Download historical candles for one futures instrument via KiteConnect.

    Args:
        continuous: True for daily (stitched across expired contracts),
                    False for intraday (per-contract data only).

    Always includes oi=True for open interest data.
    Handles chunking and rate limiting. Returns list of candle dicts.
    """
    kite_interval = INTERVAL_MAP[interval]
    chunks = _chunk_date_range(start_date, end_date, interval)
    all_candles: list[dict] = []

    for i, (chunk_start, chunk_end) in enumerate(chunks):
        from_dt = datetime(chunk_start.year, chunk_start.month, chunk_start.day, 0, 0, 0)
        to_dt = datetime(chunk_end.year, chunk_end.month, chunk_end.day, 23, 59, 59)

        candles = kite.historical_data(
            token, from_dt, to_dt, kite_interval,
            continuous=continuous, oi=True,
        )
        all_candles.extend(candles)

        # Rate limit between API calls
        if i < len(chunks) - 1:
            time.sleep(RATE_LIMIT_SECS)

    return all_candles


# ---------------------------------------------------------------------------
# DB operations (async)
# ---------------------------------------------------------------------------

async def _ensure_tables(pool) -> None:
    """Auto-create futures backtest tables if they don't exist (self-healing).

    Also handles schema migration: if the old schema (without tradingsymbol
    column) is detected, drops and recreates both tables. Safe because only
    ~748 daily candles exist at this point — quick to re-download.
    """
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT EXISTS ("
            "  SELECT FROM information_schema.tables "
            "  WHERE table_name = 'backtest_futures_candles'"
            ")"
        )
        if not exists:
            await conn.execute(_FUTURES_TABLES_SQL)
            step_done("Created backtest_futures_candles + backtest_futures_metadata tables")
            return

        # Check if schema has the tradingsymbol column (added in CC004)
        has_col = await conn.fetchval(
            "SELECT EXISTS ("
            "  SELECT FROM information_schema.columns "
            "  WHERE table_name = 'backtest_futures_candles' "
            "  AND column_name = 'tradingsymbol'"
            ")"
        )
        if not has_col:
            step_info("Old schema detected — migrating (DROP + RECREATE)...")
            await conn.execute("DROP TABLE IF EXISTS backtest_futures_candles CASCADE")
            await conn.execute("DROP TABLE IF EXISTS backtest_futures_metadata CASCADE")
            await conn.execute(_FUTURES_TABLES_SQL)
            step_done("Recreated futures tables with tradingsymbol + expiry columns")


async def _get_metadata(pool, instrument: str, interval: str) -> dict | None:
    """Check backtest_futures_metadata for existing download info."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT first_candle, last_candle, candle_count "
            "FROM backtest_futures_metadata "
            "WHERE instrument = $1 AND interval = $2",
            instrument, interval,
        )
        if row:
            return dict(row)
    return None


async def _insert_candles(
    pool,
    instrument: str,
    interval: str,
    candles: list[dict],
    *,
    tradingsymbol: str = "",
    expiry: date | None = None,
) -> int:
    """Batch insert candles with ON CONFLICT DO NOTHING. Returns count inserted.

    Args:
        tradingsymbol: Contract name (e.g. 'NIFTY26MARFUT'). Empty for daily continuous.
        expiry: Contract expiry date. None for daily continuous.
    """
    if not candles:
        return 0

    sql = (
        "INSERT INTO backtest_futures_candles "
        "(instrument, tradingsymbol, expiry, interval, timestamp, "
        "open, high, low, close, volume, oi) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) "
        "ON CONFLICT DO NOTHING"
    )

    rows = []
    for c in candles:
        candle_dt = c["date"]
        # KiteConnect returns naive datetimes in IST — localize
        if candle_dt.tzinfo is None:
            candle_dt = IST.localize(candle_dt)
        rows.append((
            instrument, tradingsymbol, expiry, interval, candle_dt,
            float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]),
            int(c["volume"]),
            int(c.get("oi", 0)) if c.get("oi") else None,
        ))

    async with pool.acquire() as conn:
        await conn.executemany(sql, rows)

    return len(rows)


async def _update_metadata(
    pool,
    instrument: str,
    interval: str,
    first_candle: date,
    last_candle: date,
    candle_count: int,
    lot_size: int,
) -> None:
    """Upsert download metadata using LEAST/GREATEST for date ranges."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO backtest_futures_metadata "
            "(instrument, interval, first_candle, last_candle, candle_count, "
            "lot_size, last_download) "
            "VALUES ($1, $2, $3, $4, $5, $6, NOW()) "
            "ON CONFLICT ON CONSTRAINT backtest_futures_metadata_pkey "
            "DO UPDATE SET "
            "first_candle = LEAST(backtest_futures_metadata.first_candle, $3), "
            "last_candle = GREATEST(backtest_futures_metadata.last_candle, $4), "
            "candle_count = backtest_futures_metadata.candle_count + $5, "
            "lot_size = $6, last_download = NOW()",
            instrument, interval, first_candle, last_candle,
            candle_count, lot_size,
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def _download_and_store(
    kite,
    pool,
    all_contracts: list[dict],
    interval: str,
    days: int,
) -> dict:
    """Download one interval for all resolved futures instruments.

    Dual-mode logic:
    - day interval: continuous=True, one token per instrument (nearest expiry)
    - 5min/15min: continuous=False, each contract downloaded separately

    Returns summary dict: {interval, downloaded, skipped, total_candles, failed}
    """
    end_date = date.today()
    start_date = end_date - timedelta(days=days)
    downloaded = 0
    skipped = 0
    total_candles = 0
    failed: list[dict] = []

    if interval == "day":
        # Mode 1: Daily continuous — use nearest contract per instrument
        targets = _pick_nearest_per_instrument(all_contracts)
        use_continuous = True
    else:
        # Mode 2: Intraday — download each contract separately
        targets = all_contracts
        use_continuous = False

    total = len(targets)

    for idx, inst in enumerate(targets, 1):
        name = inst["name"]
        token = inst["token"]
        lot_size = inst["lot_size"]
        tsym = inst.get("tradingsymbol", "")
        inst_expiry = inst.get("expiry")

        # For daily continuous, use empty tradingsymbol / no expiry
        if use_continuous:
            store_tsym = ""
            store_expiry = None
            label = name
        else:
            store_tsym = tsym
            store_expiry = inst_expiry
            label = f"{name} ({tsym})"

        try:
            # For intraday per-contract, compute start date from contract listing
            if use_continuous:
                actual_start = start_date
            else:
                # Contracts listed ~90 days before expiry
                contract_start = inst_expiry - timedelta(days=90) if inst_expiry else start_date
                actual_start = max(start_date, contract_start)

            # Check resume point (keyed on instrument+interval for aggregate tracking)
            meta = await _get_metadata(pool, name, interval)
            if use_continuous and meta and meta["first_candle"] and meta["last_candle"]:
                first_dt = meta["first_candle"]
                last_dt = meta["last_candle"]
                first_d = first_dt.date() if hasattr(first_dt, "date") else first_dt
                last_d = last_dt.date() if hasattr(last_dt, "date") else last_dt
                if first_d <= actual_start and last_d >= end_date:
                    step_info(f"[{idx}/{total}] {label} {interval} — already complete, skipping")
                    skipped += 1
                    continue

            # Resume from last downloaded (daily continuous only)
            if use_continuous and meta and meta["last_candle"]:
                last_dt = meta["last_candle"]
                last_d = last_dt.date() if hasattr(last_dt, "date") else last_dt
                resume_from = last_d + timedelta(days=1)
                if resume_from > actual_start:
                    actual_start = resume_from

            if actual_start > end_date:
                step_info(f"[{idx}/{total}] {label} {interval} — up to date")
                skipped += 1
                continue

            # Download
            with spinner(
                f"[{idx}/{total}] {label} — downloading {interval} "
                f"({actual_start} → {end_date})..."
            ):
                candles = _download_futures(
                    kite, token, name, interval, actual_start, end_date,
                    continuous=use_continuous,
                )

            if candles:
                count = await _insert_candles(
                    pool, name, interval, candles,
                    tradingsymbol=store_tsym, expiry=store_expiry,
                )
                first_dt = candles[0]["date"]
                last_dt = candles[-1]["date"]
                first_date = first_dt.date() if hasattr(first_dt, "date") else first_dt
                last_date = last_dt.date() if hasattr(last_dt, "date") else last_dt
                await _update_metadata(
                    pool, name, interval, first_date, last_date, count, lot_size,
                )
                total_candles += count
                step_done(f"{label} — {count:,} candles ({first_date} → {last_date})")
            else:
                step_info(f"{label} — no candles returned")

            downloaded += 1

        except Exception as exc:
            step_fail(f"{label} — {type(exc).__name__}: {exc}")
            failed.append({"instrument": name, "error": str(exc)})

    return {
        "interval": interval,
        "downloaded": downloaded,
        "skipped": skipped,
        "total_candles": total_candles,
        "failed": failed,
    }


async def _show_status(pool) -> None:
    """Query and display futures download coverage per interval."""
    async with pool.acquire() as conn:
        # Aggregate metadata
        meta_rows = await conn.fetch(
            "SELECT instrument, interval, candle_count, "
            "first_candle, last_candle, lot_size, last_download "
            "FROM backtest_futures_metadata "
            "ORDER BY instrument, interval"
        )
        # Per-contract breakdown for intraday
        contract_rows = await conn.fetch(
            "SELECT instrument, tradingsymbol, expiry, interval, "
            "COUNT(*) as candles, MIN(timestamp) as first_ts, MAX(timestamp) as last_ts "
            "FROM backtest_futures_candles "
            "WHERE tradingsymbol != '' "
            "GROUP BY instrument, tradingsymbol, expiry, interval "
            "ORDER BY instrument, interval, expiry"
        )

    if not meta_rows:
        print("\nFutures Data Status")
        print("=" * 60)
        print("  No futures data downloaded yet.")
        print("  Run: tradeos futures download --all")
        print()
        return

    print("\nFutures Data Status — Aggregate")
    print("=" * 90)
    print(
        f"  {'Instrument':<12s} {'Interval':<10s} {'Candles':>10s} "
        f"{'From':>12s} {'To':>12s} {'Lot':>5s} {'Last Download':>20s}"
    )
    print("  " + "-" * 86)

    grand_candles = 0
    for row in meta_rows:
        candles = row["candle_count"] or 0
        grand_candles += candles
        first = str(row["first_candle"].date()) if row["first_candle"] else "—"
        last = str(row["last_candle"].date()) if row["last_candle"] else "—"
        lot = str(row["lot_size"]) if row["lot_size"] else "—"
        last_dl = (
            row["last_download"].strftime("%Y-%m-%d %H:%M")
            if row["last_download"] else "—"
        )
        print(
            f"  {row['instrument']:<12s} {row['interval']:<10s} {candles:>10,d} "
            f"{first:>12s} {last:>12s} {lot:>5s} {last_dl:>20s}"
        )

    print("  " + "-" * 86)
    print(f"  Total: {grand_candles:,} candles across {len(meta_rows)} instrument-interval pairs")

    # Per-contract breakdown
    if contract_rows:
        print(f"\nFutures Data Status — Per Contract (Intraday)")
        print("=" * 90)
        print(
            f"  {'Instrument':<12s} {'Contract':<22s} {'Expiry':<12s} "
            f"{'Interval':<10s} {'Candles':>10s} {'From':>12s} {'To':>12s}"
        )
        print("  " + "-" * 86)
        for row in contract_rows:
            first = str(row["first_ts"].date()) if row["first_ts"] else "—"
            last = str(row["last_ts"].date()) if row["last_ts"] else "—"
            expiry = str(row["expiry"]) if row["expiry"] else "—"
            print(
                f"  {row['instrument']:<12s} {row['tradingsymbol']:<22s} {expiry:<12s} "
                f"{row['interval']:<10s} {row['candles']:>10,d} {first:>12s} {last:>12s}"
            )

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

    # Auth check
    with spinner("Verifying KiteConnect auth..."):
        kite = _init_kite()
    step_done("KiteConnect authenticated")

    # Resolve ALL active futures contracts
    with spinner("Resolving futures contracts from NFO..."):
        resolved = _resolve_futures_contracts(kite, config)

    if not resolved:
        print("ERROR: No futures contracts resolved. Check config/settings.yaml futures section.")
        return 1

    # Group for display
    from itertools import groupby
    for name, grp in groupby(resolved, key=lambda r: r["name"]):
        contracts = list(grp)
        step_done(f"{name}: {len(contracts)} contract(s)")
        for c in contracts:
            step_info(f"  {c['tradingsymbol']} (token={c['token']}, lot={c['lot_size']}, expiry={c['expiry']})")

    # Filter to single instrument if specified
    if args.instrument:
        match = [r for r in resolved if r["name"].upper() == args.instrument.upper()]
        if not match:
            print(f"ERROR: Instrument '{args.instrument}' not found. Available: {', '.join(r['name'] for r in resolved)}")
            return 1
        resolved = match

    pool = await asyncpg.create_pool(dsn, min_size=2, max_size=5)

    try:
        # Self-healing table creation
        await _ensure_tables(pool)

        if args.all:
            # Download all intervals
            order = ["day", "15min", "5min"]
            grand_total = 0
            grand_failed: list[dict] = []

            for i, interval in enumerate(order, 1):
                days = DEFAULT_DAYS[interval]
                print(f"\n{'='*60}")
                print(f"Interval {i}/{len(order)}: {interval} ({days} days)")
                print(f"{'='*60}")
                result = await _download_and_store(kite, pool, resolved, interval, days)
                grand_total += result["total_candles"]
                grand_failed.extend(result["failed"])

            print(f"\n{'='*60}")
            step_done(f"All intervals complete — {grand_total:,} total candles")
            if grand_failed:
                step_fail(
                    f"{len(grand_failed)} failures: "
                    f"{', '.join(f['instrument'] for f in grand_failed)}"
                )
        else:
            # Single interval mode
            interval = args.interval
            days = args.days

            print(f"\nDownloading {interval} candles ({days} days) for {len(resolved)} instruments...")
            result = await _download_and_store(kite, pool, resolved, interval, days)

            print()
            step_done(
                f"{interval} complete — {result['downloaded']} downloaded, "
                f"{result['skipped']} skipped, {result['total_candles']:,} candles"
            )
            if result["failed"]:
                step_fail(
                    f"{len(result['failed'])} failures: "
                    f"{', '.join(f['instrument'] for f in result['failed'])}"
                )
    finally:
        await pool.close()

    return 0


async def _run_status(args) -> int:
    """Async entry: show futures download coverage."""
    import asyncpg

    dsn = _load_dsn()
    if not dsn:
        print("ERROR: No database DSN found in config/settings.yaml")
        return 1

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        await _ensure_tables(pool)
        await _show_status(pool)
    finally:
        await pool.close()

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="futures_data_downloader",
        description="TradeOS Futures Historical Data Downloader (NIFTY/BANKNIFTY)",
    )
    sub = parser.add_subparsers(dest="command")

    # download subcommand
    dl = sub.add_parser("download", help="Download futures historical candles from KiteConnect")
    dl.add_argument(
        "--interval",
        choices=list(INTERVAL_MAP.keys()),
        default="15min",
        help="Candle interval (default: 15min)",
    )
    dl.add_argument(
        "--days",
        type=int,
        default=548,
        help="Number of days to download (default: 548 ≈ 18 months)",
    )
    dl.add_argument(
        "--instrument",
        type=str,
        help="Download a single instrument (NIFTY or BANKNIFTY)",
    )
    dl.add_argument(
        "--all",
        action="store_true",
        help="Download ALL 3 intervals with 18-month depth",
    )

    # status subcommand
    sub.add_parser("status", help="Show futures download coverage per interval")

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
