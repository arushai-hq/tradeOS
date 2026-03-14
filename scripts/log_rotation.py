#!/usr/bin/env python3
"""
TradeOS — Log Rotation

Compresses log files older than 30 days to .gz.
Deletes compressed archives older than 90 days.

Scans all subdirectories under logs/ (tradeos/, hawk/, token/).
Ignores .gitkeep and non-.log files.

Usage:
    python scripts/log_rotation.py              # Default: 30-day compress, 90-day delete
    python scripts/log_rotation.py --dry-run     # Show what would be done

Cron:  0 2 * * 0 cd /opt/tradeOS && .venv/bin/python scripts/log_rotation.py
"""
from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
LOGS_DIR = ROOT / "logs"
SETTINGS_FILE = ROOT / "config" / "settings.yaml"

# Defaults (overridden by settings.yaml log_rotation section)
DEFAULT_COMPRESS_AFTER_DAYS = 30
DEFAULT_DELETE_AFTER_DAYS = 90


def _load_rotation_config() -> dict:
    """Load log_rotation config from settings.yaml with defaults fallback."""
    try:
        with open(SETTINGS_FILE) as f:
            cfg = yaml.safe_load(f) or {}
        rotation = cfg.get("log_rotation", {})
        return {
            "compress_after_days": rotation.get("compress_after_days", DEFAULT_COMPRESS_AFTER_DAYS),
            "delete_after_days": rotation.get("delete_after_days", DEFAULT_DELETE_AFTER_DAYS),
        }
    except Exception:
        return {
            "compress_after_days": DEFAULT_COMPRESS_AFTER_DAYS,
            "delete_after_days": DEFAULT_DELETE_AFTER_DAYS,
        }


def compress_file(path: Path) -> Path:
    """Compress a file to .gz and remove the original. Returns .gz path."""
    gz_path = path.with_suffix(path.suffix + ".gz")
    with open(path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    path.unlink()
    return gz_path


def run_rotation(dry_run: bool = False) -> dict:
    """Scan logs/ subdirectories and apply rotation policy.

    Returns dict with counts: {"compressed": N, "deleted": N, "errors": N}
    """
    config = _load_rotation_config()
    compress_days = config["compress_after_days"]
    delete_days = config["delete_after_days"]

    now = datetime.now()
    compress_cutoff = now - timedelta(days=compress_days)
    delete_cutoff = now - timedelta(days=delete_days)

    stats = {"compressed": 0, "deleted": 0, "errors": 0}

    if not LOGS_DIR.exists():
        return stats

    for log_file in sorted(LOGS_DIR.rglob("*")):
        if not log_file.is_file():
            continue
        if log_file.name == ".gitkeep":
            continue

        try:
            mtime = datetime.fromtimestamp(log_file.stat().st_mtime)
        except OSError:
            continue

        # Delete old .gz archives
        if log_file.suffix == ".gz" and mtime < delete_cutoff:
            if dry_run:
                print(f"  [DELETE] {log_file.relative_to(ROOT)}")
            else:
                log_file.unlink()
            stats["deleted"] += 1
            continue

        # Compress old .log files
        if log_file.suffix == ".log" and mtime < compress_cutoff:
            if dry_run:
                print(f"  [COMPRESS] {log_file.relative_to(ROOT)}")
            else:
                try:
                    compress_file(log_file)
                except Exception as exc:
                    print(f"  ERROR compressing {log_file}: {exc}", file=sys.stderr)
                    stats["errors"] += 1
                    continue
            stats["compressed"] += 1

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TradeOS log rotation: compress old logs, delete archives.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    args = parser.parse_args()

    print("=== TradeOS Log Rotation ===")
    config = _load_rotation_config()
    print(f"  Compress after: {config['compress_after_days']} days")
    print(f"  Delete after:   {config['delete_after_days']} days")
    if args.dry_run:
        print("  Mode: DRY RUN")
    print()

    stats = run_rotation(dry_run=args.dry_run)

    print(f"\nResults: {stats['compressed']} compressed, "
          f"{stats['deleted']} deleted, {stats['errors']} errors")


if __name__ == "__main__":
    main()
