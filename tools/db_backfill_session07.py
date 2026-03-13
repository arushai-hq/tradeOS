#!/usr/bin/env python3
"""
TradeOS — Session 07 Data Backfill Script

Fixes historical data issues from Session 07 (2026-03-13):
  1. Trade exit prices (were entry_price due to B12 bug)
  2. Trade signal_id links
  3. Historical PENDING signals that should be FILLED or REJECTED

Usage:
    python tools/db_backfill_session07.py --dry-run   # Preview changes
    python tools/db_backfill_session07.py --execute    # Apply changes
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import asyncpg
import yaml

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from risk_manager.charge_calculator import ChargeCalculator


# -----------------------------------------------------------------------
# Session 07 known trade data
# -----------------------------------------------------------------------

SESSION_DATE = date(2026, 3, 13)

TRADE_FIXES = [
    {
        "trade_id": 3,
        "symbol": "SUNPHARMA",
        "instrument_token": 857857,
        "direction": "SHORT",
        "entry_price": Decimal("1825.50"),
        "exit_price": Decimal("1805.10"),
        "qty": 71,
        "signal_id": 20,
    },
    {
        "trade_id": 4,
        "symbol": "TITAN",
        "instrument_token": 897537,
        "direction": "SHORT",
        "entry_price": Decimal("4093.80"),
        "exit_price": Decimal("4091.00"),
        "qty": 32,
        "signal_id": 23,
    },
]


def _load_dsn() -> str:
    """Load DB DSN from config/settings.yaml."""
    config_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)
    return str(config.get("database", {}).get("dsn", ""))


async def _backfill(execute: bool) -> None:
    dsn = _load_dsn()
    if not dsn:
        print("ERROR: No database DSN found in config/settings.yaml")
        sys.exit(1)

    pool = await asyncpg.create_pool(dsn)
    calc = ChargeCalculator()
    changes: list[str] = []

    try:
        # ---------------------------------------------------------------
        # 1. Fix Session 07 trade exit prices + P&L
        # ---------------------------------------------------------------
        print("\n=== Step 1: Fix Session 07 trades ===")
        for fix in TRADE_FIXES:
            entry = fix["entry_price"]
            exit_p = fix["exit_price"]
            qty = fix["qty"]
            direction = fix["direction"]

            if direction == "SHORT":
                gross_pnl = (entry - exit_p) * Decimal(str(qty))
            else:
                gross_pnl = (exit_p - entry) * Decimal(str(qty))

            breakdown = calc.calculate(qty, entry, exit_p, direction)
            charges = breakdown.total
            net_pnl = gross_pnl - charges
            position_value = Decimal(str(qty)) * entry
            pnl_pct = net_pnl / position_value if position_value else Decimal("0")

            desc = (
                f"  Trade #{fix['trade_id']} ({fix['symbol']} {direction}): "
                f"exit={float(exit_p):.2f}, gross={float(gross_pnl):.2f}, "
                f"charges={float(charges):.2f}, net={float(net_pnl):.2f}, "
                f"pnl_pct={float(pnl_pct):.4f}"
            )
            print(desc)
            changes.append(desc)

            if execute:
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE trades SET
                            actual_exit = $1,
                            exit_reason = 'HARD_EXIT_1500',
                            gross_pnl = $2,
                            charges = $3,
                            net_pnl = $4,
                            pnl_pct = $5
                        WHERE id = $6
                        """,
                        float(exit_p),
                        float(gross_pnl),
                        float(charges),
                        float(net_pnl),
                        float(pnl_pct),
                        fix["trade_id"],
                    )

        # ---------------------------------------------------------------
        # 2. Fix signal_id links
        # ---------------------------------------------------------------
        print("\n=== Step 2: Fix signal_id links ===")
        for fix in TRADE_FIXES:
            desc = f"  Trade #{fix['trade_id']} → signal_id={fix['signal_id']}"
            print(desc)
            changes.append(desc)

            if execute:
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE trades SET signal_id = $1 WHERE id = $2",
                        fix["signal_id"],
                        fix["trade_id"],
                    )

        # ---------------------------------------------------------------
        # 3. Fix historical PENDING signals
        # ---------------------------------------------------------------
        print("\n=== Step 3: Fix historical PENDING signals ===")
        async with pool.acquire() as conn:
            # Signals with matching trades → FILLED
            filled_rows = await conn.fetch(
                """
                SELECT s.id, s.symbol, s.session_date
                FROM signals s
                JOIN trades t ON s.session_date = t.session_date AND s.symbol = t.symbol
                WHERE s.status = 'PENDING' AND s.session_date < $1
                """,
                date.today(),
            )
            for row in filled_rows:
                desc = (
                    f"  Signal #{row['id']} ({row['symbol']}, "
                    f"{row['session_date']}) → FILLED"
                )
                print(desc)
                changes.append(desc)

            if execute and filled_rows:
                ids = [r["id"] for r in filled_rows]
                await conn.execute(
                    "UPDATE signals SET status = 'FILLED' WHERE id = ANY($1::int[])",
                    ids,
                )

            # Remaining PENDING signals from past sessions → REJECTED
            rejected_rows = await conn.fetch(
                """
                SELECT id, symbol, session_date
                FROM signals
                WHERE status = 'PENDING' AND session_date < $1
                """,
                date.today(),
            )
            for row in rejected_rows:
                desc = (
                    f"  Signal #{row['id']} ({row['symbol']}, "
                    f"{row['session_date']}) → REJECTED (SIZER_REJECTED)"
                )
                print(desc)
                changes.append(desc)

            if execute and rejected_rows:
                ids = [r["id"] for r in rejected_rows]
                await conn.execute(
                    """
                    UPDATE signals SET status = 'REJECTED', reject_reason = 'SIZER_REJECTED'
                    WHERE id = ANY($1::int[])
                    """,
                    ids,
                )

        # ---------------------------------------------------------------
        # Summary
        # ---------------------------------------------------------------
        print(f"\n=== Summary: {len(changes)} changes ===")
        if execute:
            print("All changes APPLIED.")
        else:
            print("DRY RUN — no changes written. Use --execute to apply.")

    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill Session 07 trade data in TimescaleDB",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Preview changes only")
    group.add_argument("--execute", action="store_true", help="Apply changes to DB")
    args = parser.parse_args()

    asyncio.run(_backfill(execute=args.execute))


if __name__ == "__main__":
    main()
