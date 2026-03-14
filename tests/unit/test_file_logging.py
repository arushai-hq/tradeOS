"""
Tests for date-based file logging and log rotation.

Covers:
  (a) _configure_structlog creates dated log file in correct directory
  (b) get_current_log_path() returns the absolute path after configuration
  (c) Log file content matches ConsoleRenderer format (session_report.py compatible)
  (d) log_rotation compress_file creates .gz and removes original
  (e) log_rotation respects age thresholds (compress old, delete ancient .gz)
"""
from __future__ import annotations

import gzip
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import structlog


# ---------------------------------------------------------------------------
# (a) _configure_structlog creates dated log file
# ---------------------------------------------------------------------------

class TestConfigureStructlog:
    """Tests for main.py _configure_structlog and get_current_log_path."""

    def _reset_structlog(self):
        """Reset structlog and stdlib logging state between tests."""
        structlog.reset_defaults()
        root = logging.getLogger()
        root.handlers.clear()

    def test_log_file_created(self, tmp_path):
        """(a) _configure_structlog creates the dated log file."""
        self._reset_structlog()
        original_dir = os.getcwd()
        try:
            os.chdir(tmp_path)

            # Re-import to pick up fresh state
            import importlib
            import main as main_mod
            importlib.reload(main_mod)

            main_mod._configure_structlog(
                dev_mode=True, log_subdir="tradeos", log_prefix="tradeos",
            )

            log_path = main_mod.get_current_log_path()
            assert log_path != ""
            assert "tradeos" in log_path

            # Trigger a log write
            log = structlog.get_logger()
            log.info("test_event", key="value")

            assert os.path.exists(log_path)
            assert os.path.getsize(log_path) > 0
        finally:
            os.chdir(original_dir)
            self._reset_structlog()


# ---------------------------------------------------------------------------
# (b) get_current_log_path returns absolute path
# ---------------------------------------------------------------------------

class TestGetCurrentLogPath:
    """Tests for get_current_log_path()."""

    def _reset_structlog(self):
        structlog.reset_defaults()
        root = logging.getLogger()
        root.handlers.clear()

    def test_get_current_log_path_returns_absolute(self, tmp_path):
        """(b) get_current_log_path returns an absolute path with correct pattern."""
        self._reset_structlog()
        original_dir = os.getcwd()
        try:
            os.chdir(tmp_path)

            import importlib
            import main as main_mod
            importlib.reload(main_mod)

            main_mod._configure_structlog(
                dev_mode=True, log_subdir="test_sub", log_prefix="test",
            )

            path = main_mod.get_current_log_path()
            assert os.path.isabs(path)
            assert "test_sub" in path

            today_str = datetime.now().strftime("%Y-%m-%d")
            assert today_str in path
        finally:
            os.chdir(original_dir)
            self._reset_structlog()


# ---------------------------------------------------------------------------
# (c) Log file format matches session_report.py expectations
# ---------------------------------------------------------------------------

class TestLogFileFormat:
    """Verify log file output is parseable by session_report.py."""

    def _reset_structlog(self):
        structlog.reset_defaults()
        root = logging.getLogger()
        root.handlers.clear()

    def test_log_file_format_matches_session_report(self, tmp_path):
        """(c) File output is ConsoleRenderer format parseable by session_report.py."""
        self._reset_structlog()
        original_dir = os.getcwd()
        try:
            os.chdir(tmp_path)

            import importlib
            import main as main_mod
            importlib.reload(main_mod)

            main_mod._configure_structlog(
                dev_mode=True, log_subdir="tradeos", log_prefix="tradeos",
            )

            log = structlog.get_logger()
            log.info("startup_token_valid", user_id="XP8470")

            from tools.session_report import parse_line

            log_path = main_mod.get_current_log_path()
            with open(log_path, encoding="utf-8") as f:
                for line in f:
                    parsed = parse_line(line)
                    if parsed and parsed["event"] == "startup_token_valid":
                        assert parsed["level"] == "info"
                        assert parsed["fields"]["user_id"] == "XP8470"
                        return

            pytest.fail("startup_token_valid event not found in log file")
        finally:
            os.chdir(original_dir)
            self._reset_structlog()


# ---------------------------------------------------------------------------
# (d) log_rotation compress_file
# ---------------------------------------------------------------------------

class TestLogRotationCompress:
    """Tests for scripts/log_rotation.py compress_file."""

    def test_compress_file(self, tmp_path):
        """(d) compress_file creates .gz and removes original."""
        from scripts.log_rotation import compress_file

        log_file = tmp_path / "test.log"
        log_file.write_text("hello world\n")
        assert log_file.exists()

        gz_path = compress_file(log_file)
        assert gz_path.suffix == ".gz"
        assert gz_path.exists()
        assert not log_file.exists()

        # Verify content decompresses correctly
        with gzip.open(gz_path, "rt") as f:
            assert f.read() == "hello world\n"


# ---------------------------------------------------------------------------
# (e) log_rotation age thresholds
# ---------------------------------------------------------------------------

class TestLogRotationThresholds:
    """Tests for run_rotation age-based thresholds."""

    def test_rotation_age_thresholds(self, tmp_path):
        """(e) run_rotation compresses old files and deletes ancient .gz."""
        from scripts.log_rotation import run_rotation

        # Create test directory structure
        subdir = tmp_path / "tradeos"
        subdir.mkdir(parents=True)

        # Recent file (should not be touched)
        recent = subdir / "recent.log"
        recent.write_text("recent log")

        # Old file (>30 days, should be compressed)
        old = subdir / "old.log"
        old.write_text("old log")
        old_mtime = time.time() - (35 * 86400)
        os.utime(old, (old_mtime, old_mtime))

        # Ancient .gz (>90 days, should be deleted)
        ancient_gz = subdir / "ancient.log.gz"
        ancient_gz.write_bytes(b"fake gz")
        ancient_mtime = time.time() - (95 * 86400)
        os.utime(ancient_gz, (ancient_mtime, ancient_mtime))

        with patch("scripts.log_rotation.LOGS_DIR", tmp_path), \
             patch("scripts.log_rotation._load_rotation_config", return_value={
                 "compress_after_days": 30,
                 "delete_after_days": 90,
             }):
            stats = run_rotation(dry_run=False)

        assert stats["compressed"] == 1
        assert stats["deleted"] == 1
        assert stats["errors"] == 0

        # Verify: recent untouched, old compressed, ancient deleted
        assert recent.exists()
        assert not old.exists()
        assert (subdir / "old.log.gz").exists()
        assert not ancient_gz.exists()

    def test_rotation_recent_untouched(self, tmp_path):
        """Recent files (<30 days) are not compressed or deleted."""
        from scripts.log_rotation import run_rotation

        subdir = tmp_path / "tradeos"
        subdir.mkdir(parents=True)

        recent1 = subdir / "today.log"
        recent1.write_text("today")
        recent2 = subdir / "yesterday.log"
        recent2.write_text("yesterday")
        # Set yesterday's mtime to 1 day ago
        yesterday_mtime = time.time() - 86400
        os.utime(recent2, (yesterday_mtime, yesterday_mtime))

        with patch("scripts.log_rotation.LOGS_DIR", tmp_path), \
             patch("scripts.log_rotation._load_rotation_config", return_value={
                 "compress_after_days": 30,
                 "delete_after_days": 90,
             }):
            stats = run_rotation(dry_run=False)

        assert stats["compressed"] == 0
        assert stats["deleted"] == 0
        assert recent1.exists()
        assert recent2.exists()
