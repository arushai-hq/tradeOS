"""
CLI progress indicators — spinner + step tracker.

Reusable progress utility for long-running TradeOS CLI commands.
Uses only stdlib: threading, sys, time, itertools.

Features:
  - Spinner animation for indeterminate-length operations
  - Respects NO_COLOR env var (text-only when set)
  - Only renders on interactive terminals (sys.stderr.isatty())
  - Output on stderr so stdout stays clean for piping
  - Context manager interface: with spinner("Fetching data..."):

Usage:
    from utils.progress import spinner, step_done, step_fail

    with spinner("Fetching market data..."):
        data = fetch_data()
    step_done("Market data: 48 stocks, 8 sectors")

    with spinner("Analyzing with Claude..."):
        result = call_llm()
    step_done("Claude: 10 picks")
"""
from __future__ import annotations

import itertools
import os
import sys
import threading
import time
from contextlib import contextmanager

# Braille spinner frames
_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

_no_color = bool(os.environ.get("NO_COLOR"))
_is_tty = sys.stderr.isatty()


def _can_animate() -> bool:
    """Check if animated output is possible."""
    return _is_tty and not _no_color


def _write(text: str) -> None:
    """Write to stderr without newline."""
    sys.stderr.write(text)
    sys.stderr.flush()


def _clear_line() -> None:
    """Clear the current line on stderr."""
    if _is_tty:
        _write("\r\033[K")


class Spinner:
    """Threaded spinner animation for indeterminate-length operations."""

    def __init__(self, message: str) -> None:
        self._message = message
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not _can_animate():
            if _is_tty:
                _write(f"  {self._message}\n")
            return
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self) -> None:
        for frame in itertools.cycle(_FRAMES):
            if self._stop_event.is_set():
                break
            _clear_line()
            _write(f"  {frame} {self._message}")
            time.sleep(0.08)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1)
        _clear_line()


@contextmanager
def spinner(message: str):
    """Context manager that shows a spinner while the block executes.

    Usage:
        with spinner("Fetching data..."):
            result = slow_operation()
        step_done("Fetched 48 stocks")
    """
    s = Spinner(message)
    s.start()
    try:
        yield
    finally:
        s.stop()


def step_done(message: str) -> None:
    """Print a completed step with checkmark."""
    if not _is_tty:
        return
    if _no_color:
        _write(f"  OK {message}\n")
    else:
        _write(f"  \033[32m✅\033[0m {message}\n")


def step_fail(message: str) -> None:
    """Print a failed step with X mark."""
    if not _is_tty:
        return
    if _no_color:
        _write(f"  FAIL {message}\n")
    else:
        _write(f"  \033[31m❌\033[0m {message}\n")


def step_info(message: str) -> None:
    """Print an info step (neutral, for status updates)."""
    if not _is_tty:
        return
    _write(f"  {message}\n")
