"""
Tests for CLI progress spinner utility.

Tests:
  (a) spinner context manager works without crash
  (b) NO_COLOR env var disables animation
  (c) non-TTY stderr produces no output
  (d) step_done/step_fail produce correct output on TTY
"""
from __future__ import annotations

import io
import os
import sys

import pytest


def test_spinner_context_manager():
    """Spinner context manager runs without error."""
    from utils.progress import spinner

    with spinner("Testing..."):
        pass  # No-op, just verify no crash


def test_spinner_non_tty_no_output(monkeypatch):
    """Spinner produces no output when stderr is not a TTY."""
    fake_stderr = io.StringIO()
    fake_stderr.isatty = lambda: False
    monkeypatch.setattr(sys, "stderr", fake_stderr)
    # Force re-evaluation of _is_tty
    monkeypatch.setattr("utils.progress._is_tty", False)

    from utils.progress import spinner, step_done

    with spinner("Testing..."):
        pass
    step_done("Done")

    assert fake_stderr.getvalue() == ""


def test_step_done_tty_output(monkeypatch):
    """step_done produces output on TTY."""
    fake_stderr = io.StringIO()
    fake_stderr.isatty = lambda: True
    monkeypatch.setattr(sys, "stderr", fake_stderr)
    monkeypatch.setattr("utils.progress._is_tty", True)
    monkeypatch.setattr("utils.progress._no_color", False)

    from utils.progress import step_done

    step_done("Market data: 48 stocks")

    output = fake_stderr.getvalue()
    assert "Market data: 48 stocks" in output
    assert "✅" in output


def test_step_fail_tty_output(monkeypatch):
    """step_fail produces output on TTY."""
    fake_stderr = io.StringIO()
    fake_stderr.isatty = lambda: True
    monkeypatch.setattr(sys, "stderr", fake_stderr)
    monkeypatch.setattr("utils.progress._is_tty", True)
    monkeypatch.setattr("utils.progress._no_color", False)

    from utils.progress import step_fail

    step_fail("Connection failed")

    output = fake_stderr.getvalue()
    assert "Connection failed" in output
    assert "❌" in output


def test_no_color_disables_emoji(monkeypatch):
    """NO_COLOR produces text-only output."""
    fake_stderr = io.StringIO()
    fake_stderr.isatty = lambda: True
    monkeypatch.setattr(sys, "stderr", fake_stderr)
    monkeypatch.setattr("utils.progress._is_tty", True)
    monkeypatch.setattr("utils.progress._no_color", True)

    from utils.progress import step_done

    step_done("Done")

    output = fake_stderr.getvalue()
    assert "OK Done" in output
    assert "✅" not in output


def test_spinner_stops_cleanly():
    """Spinner thread stops when context exits."""
    import time
    from utils.progress import Spinner

    s = Spinner("Test")
    s.start()
    time.sleep(0.2)
    s.stop()

    # Thread should be joined
    if s._thread:
        assert not s._thread.is_alive()
