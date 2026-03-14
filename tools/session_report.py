#!/usr/bin/env python3
"""
TradeOS — Session Report Tool

Parses ANSI-colored structlog key=value session logs and prints a
6-section terminal report. Supports optional CSV and Excel export.

Supports both pre-B5 event names:
  s1_signal_generated, signal_queued, entry_filled, position_registered
and post-B5 event names (commit ca7ddc9):
  signal_accepted, signal_rejected, order_filled, position_closed

Usage (date-based log files):
    python tools/session_report.py logs/tradeos/tradeos_2026-03-14.log
    python tools/session_report.py logs/tradeos/tradeos_2026-03-14.log --export csv
    python tools/session_report.py logs/tradeos/tradeos_2026-03-14.log --export xlsx
    python tools/session_report.py logs/tradeos/tradeos_2026-03-14.log --export all
    python tools/session_report.py logs/tradeos/tradeos_2026-03-14.log --verbose

Legacy format (pre-date-logging):
    python tools/session_report.py logs/paper_session_03.log
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ---------------------------------------------------------------------------
# ANSI stripping + line parser
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_LINE_RE = re.compile(
    r"^(?P<ts>\S+)\s+\[(?P<level>\w+)\s*\]\s+(?P<event>\w+)\s*(?P<rest>.*)?$"
)
# Matches key=value pairs where value is a quoted string, {dict}, [list], or bare token
_FIELD_RE = re.compile(
    r"(\w+)=('(?:[^'\\]|\\.)*'|\{[^}]*\}|\[[^\]]*\]|\S+)"
)


def strip_ansi(line: str) -> str:
    """Remove ANSI escape codes from a log line."""
    return _ANSI_RE.sub("", line)


def parse_line(raw: str) -> Optional[dict]:
    """Parse one raw log line into {ts, level, event, fields}. Returns None on no match."""
    clean = strip_ansi(raw).rstrip()
    m = _LINE_RE.match(clean)
    if not m:
        return None
    return {
        "ts": m.group("ts"),
        "level": m.group("level").strip(),
        "event": m.group("event"),
        "fields": parse_fields(m.group("rest") or ""),
    }


def _coerce(value: str):
    """Convert a raw string token to a Python type (int, float, bool, str)."""
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    if value in ("True", "true"):
        return True
    if value in ("False", "false"):
        return False
    if value in ("None", "null"):
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def parse_fields(rest: str) -> dict:
    """Parse space-separated key=value pairs from the tail of a log line."""
    result = {}
    for m in _FIELD_RE.finditer(rest):
        result[m.group(1)] = _coerce(m.group(2))
    return result


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SignalRecord:
    ts: str
    symbol: str
    direction: str
    entry: float
    stop: float
    target: float
    rsi: float
    volume_ratio: float
    status: str = "PENDING"      # ACCEPTED | BLOCKED | DEDUP
    gate: Optional[int] = None
    block_reason: str = ""


@dataclass
class TradeRecord:
    symbol: str
    direction: str
    entry_price: float
    qty: int
    stop_loss: float
    target: float
    opened_at: str = ""
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    exit_reason: str = ""
    pnl_rs: Optional[float] = None

    @property
    def status(self) -> str:
        return "CLOSED" if self.exit_price is not None else "OPEN"

    @property
    def hold_minutes(self) -> Optional[float]:
        if self.exit_time and self.opened_at:
            try:
                t_open = datetime.fromisoformat(self.opened_at)
                t_close = datetime.fromisoformat(self.exit_time)
                return (t_close - t_open).total_seconds() / 60
            except Exception:
                pass
        return None


@dataclass
class RegimeEvent:
    ts: str
    event_type: str    # "initialized" | "changed"
    old_regime: str = ""
    new_regime: str = ""
    nifty_price: float = 0.0
    vix: float = 0.0
    trigger: str = ""


@dataclass
class SessionData:
    date: str = ""
    start_ts: str = ""
    end_ts: str = ""
    mode: str = "paper"
    instruments: int = 0
    signals: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    regime_events: list = field(default_factory=list)
    heartbeat_count: int = 0
    first_heartbeat_ts: str = ""
    last_heartbeat_ts: str = ""
    last_kill_switch_level: int = 0
    last_ws_connected: bool = True
    last_open_positions: int = 0
    last_daily_pnl_pct: float = 0.0
    warnings: list = field(default_factory=list)
    hard_exit_triggered: bool = False
    hard_exit_ts: str = ""


# ---------------------------------------------------------------------------
# Session parser
# ---------------------------------------------------------------------------

class SessionParser:
    """
    Parse a TradeOS structlog file into SessionData.
    Handles both pre-B5 and post-B5 event naming conventions.

    B9 hardening:
      - Deduplicates signals and trades within a 5s window (same symbol+direction).
      - Filters ghost entries with entry_price=0.0 or qty=0.
      - Flags suspect position_closed events where qty=0 in the log event.
    """

    # Dedup window: events within this many seconds for the same (symbol, direction) are duplicates
    DEDUP_WINDOW_SECONDS: float = 5.0

    def __init__(self) -> None:
        # Pending signals awaiting resolution (ACCEPTED/BLOCKED/DEDUP)
        # Key: "symbol|direction"
        self._pending_signals: dict[str, SignalRecord] = {}
        # Open trades awaiting exit confirmation
        # Key: symbol
        self._open_trades: dict[str, TradeRecord] = {}
        # Most recent gate info from risk_gate_blocked — picked up by signal_blocked
        # Key: symbol
        self._last_gate: dict[str, dict] = {}

    def _is_duplicate_signal(self, data: SessionData, symbol: str, direction: str, ts: str) -> bool:
        """B9: Check if a signal for this (symbol, direction) was already added within the dedup window."""
        try:
            new_time = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            return False
        for sig in reversed(data.signals):
            if sig.symbol == symbol and sig.direction == direction:
                try:
                    existing_time = datetime.fromisoformat(sig.ts)
                    if abs((new_time - existing_time).total_seconds()) <= self.DEDUP_WINDOW_SECONDS:
                        return True
                except (ValueError, TypeError):
                    continue
        return False

    def _is_duplicate_trade(self, data: SessionData, symbol: str, ts: str) -> bool:
        """B9: Check if a trade for this symbol was already opened within the dedup window."""
        try:
            new_time = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            return False
        for trade in reversed(data.trades):
            if trade.symbol == symbol and trade.opened_at:
                try:
                    existing_time = datetime.fromisoformat(trade.opened_at)
                    if abs((new_time - existing_time).total_seconds()) <= self.DEDUP_WINDOW_SECONDS:
                        return True
                except (ValueError, TypeError):
                    continue
        return False

    def parse(self, filepath: str) -> SessionData:
        data = SessionData()
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for raw in f:
                parsed = parse_line(raw)
                if parsed is None:
                    continue
                self._dispatch(parsed, data)
        return data

    def _dispatch(self, parsed: dict, data: SessionData) -> None:
        ev = parsed["event"]
        ts = parsed["ts"]
        flds = parsed["fields"]
        level = parsed["level"]

        # ---- Session header ----
        if ev in ("startup_token_valid", "startup_phase1_begin"):
            if not data.start_ts:
                data.start_ts = ts
            if not data.date and "session_date" in flds:
                data.date = str(flds["session_date"])
            if "mode" in flds:
                data.mode = str(flds["mode"])

        elif ev in ("eod_shutdown_15_30", "eod_shutdown_begin", "run_trading_session_cancelled"):
            if not data.end_ts:
                data.end_ts = ts

        elif ev in ("strategy_engine_ready", "data_engine_ready"):
            if "instruments" in flds:
                data.instruments = int(flds["instruments"])

        # ---- Signals: pre-B5 ----
        elif ev == "s1_signal_generated":
            symbol = str(flds.get("symbol", ""))
            direction = str(flds.get("direction", ""))
            if self._is_duplicate_signal(data, symbol, direction, ts):
                return  # B9: skip duplicate signal within 5s window
            rec = SignalRecord(
                ts=ts,
                symbol=symbol,
                direction=direction,
                entry=float(flds.get("entry", 0)),
                stop=float(flds.get("stop", 0)),
                target=float(flds.get("target", 0)),
                rsi=float(flds.get("rsi", 0)),
                volume_ratio=float(flds.get("volume_ratio", 0)),
            )
            key = f"{symbol}|{direction}"
            self._pending_signals[key] = rec
            data.signals.append(rec)

        elif ev == "signal_queued":
            symbol = str(flds.get("symbol", ""))
            direction = str(flds.get("direction", ""))
            key = f"{symbol}|{direction}"
            if key in self._pending_signals:
                self._pending_signals[key].status = "ACCEPTED"
                del self._pending_signals[key]

        elif ev == "risk_gate_blocked":
            # Store gate info; picked up when signal_blocked fires
            symbol = str(flds.get("symbol", ""))
            self._last_gate[symbol] = {
                "gate": flds.get("gate"),
                "reason": str(flds.get("reason", "")),
            }

        elif ev == "signal_blocked":
            symbol = str(flds.get("symbol", ""))
            direction = str(flds.get("direction", ""))
            key = f"{symbol}|{direction}"
            reason = str(flds.get("reason", ""))
            gate_info = self._last_gate.pop(symbol, {})
            gate = gate_info.get("gate")
            if key in self._pending_signals:
                sig = self._pending_signals[key]
                sig.status = "BLOCKED"
                sig.block_reason = reason
                if gate is not None:
                    sig.gate = int(gate)
                del self._pending_signals[key]
            else:
                # Fall back: find most recent pending signal for this symbol
                for k in list(self._pending_signals):
                    if k.startswith(f"{symbol}|"):
                        sig = self._pending_signals[k]
                        sig.status = "BLOCKED"
                        sig.block_reason = reason
                        if gate is not None:
                            sig.gate = int(gate)
                        del self._pending_signals[k]
                        break

        elif ev == "signal_dedup_skipped":
            symbol = str(flds.get("symbol", ""))
            direction = str(flds.get("direction", ""))
            key = f"{symbol}|{direction}"
            if key in self._pending_signals:
                self._pending_signals[key].status = "DEDUP"
                del self._pending_signals[key]

        # ---- Signals: post-B5 ----
        elif ev == "signal_accepted":
            symbol = str(flds.get("symbol", ""))
            direction = str(flds.get("direction", ""))
            if self._is_duplicate_signal(data, symbol, direction, ts):
                return  # B9: skip duplicate signal within 5s window
            rec = SignalRecord(
                ts=ts,
                symbol=symbol,
                direction=direction,
                entry=float(flds.get("entry", 0)),
                stop=float(flds.get("stop", 0)),
                target=float(flds.get("target", 0)),
                rsi=float(flds.get("rsi", 0)),
                volume_ratio=float(flds.get("volume_ratio", 0)),
                status="ACCEPTED",
            )
            data.signals.append(rec)

        elif ev == "signal_rejected":
            symbol = str(flds.get("symbol", ""))
            direction = str(flds.get("direction", ""))
            if self._is_duplicate_signal(data, symbol, direction, ts):
                return  # B9: skip duplicate signal within 5s window
            rec = SignalRecord(
                ts=ts,
                symbol=symbol,
                direction=direction,
                entry=float(flds.get("entry", 0)),
                stop=float(flds.get("stop", 0)),
                target=float(flds.get("target", 0)),
                rsi=float(flds.get("rsi", 0)),
                volume_ratio=float(flds.get("volume_ratio", 0)),
                status="BLOCKED",
                block_reason=str(flds.get("reason", "")),
                gate=int(flds["gate"]) if "gate" in flds else None,
            )
            data.signals.append(rec)

        # ---- Trades: pre-B5 ----
        elif ev == "position_registered":
            symbol = str(flds.get("symbol", ""))
            entry_price = float(flds.get("entry_price", 0))
            qty = int(flds.get("qty", 0))
            # B9: filter ghost entries
            if entry_price <= 0 or qty <= 0:
                return
            # B9: deduplicate within 5s window
            if self._is_duplicate_trade(data, symbol, ts):
                return
            trade = TradeRecord(
                symbol=symbol,
                direction=str(flds.get("direction", "")),
                entry_price=entry_price,
                qty=qty,
                stop_loss=float(flds.get("stop_loss", 0)),
                target=float(flds.get("target", 0)),
                opened_at=ts,
            )
            self._open_trades[symbol] = trade
            data.trades.append(trade)

        # ---- Trades: post-B5 ----
        elif ev == "order_filled":
            symbol = str(flds.get("symbol", ""))
            entry_price = float(flds.get("fill_price", 0))
            qty = int(flds.get("qty", 0))
            # B9: filter ghost entries
            if entry_price <= 0 or qty <= 0:
                return
            # B9: deduplicate within 5s window
            if self._is_duplicate_trade(data, symbol, ts):
                return
            trade = TradeRecord(
                symbol=symbol,
                direction=str(flds.get("direction", "")),
                entry_price=entry_price,
                qty=qty,
                stop_loss=0.0,
                target=0.0,
                opened_at=ts,
            )
            self._open_trades[symbol] = trade
            data.trades.append(trade)

        elif ev == "position_closed":
            symbol = str(flds.get("symbol", ""))
            # B9: flag suspect position_closed (ghost entries had qty=0)
            event_qty = flds.get("qty")
            if event_qty is not None and int(event_qty) == 0:
                return  # Ghost position_closed — skip
            if symbol in self._open_trades:
                trade = self._open_trades[symbol]
                exit_price = float(flds.get("exit_price", 0))
                pnl_rs = float(flds.get("pnl_rs", flds.get("gross_pnl", flds.get("net_pnl", 0)))) if any(
                    k in flds for k in ("pnl_rs", "gross_pnl", "net_pnl")
                ) else None
                # B9: suspect check — pnl_points ≈ full stock price means entry_price was 0
                if pnl_rs is not None and exit_price > 0 and abs(abs(pnl_rs) - exit_price) < 1.0:
                    return  # Suspect ghost P&L — exclude
                trade.exit_price = exit_price
                trade.exit_time = ts
                trade.exit_reason = str(flds.get("exit_reason", ""))
                trade.pnl_rs = pnl_rs
                del self._open_trades[symbol]

        # ---- Regime ----
        elif ev == "regime_initialized":
            data.regime_events.append(RegimeEvent(
                ts=ts,
                event_type="initialized",
                new_regime=str(flds.get("regime", "")),
                nifty_price=float(flds.get("nifty_price", 0)),
                vix=float(flds.get("vix", 0)),
                trigger=str(flds.get("trigger", "")),
            ))

        elif ev == "regime_changed":
            data.regime_events.append(RegimeEvent(
                ts=ts,
                event_type="changed",
                old_regime=str(flds.get("old_regime", "")),
                new_regime=str(flds.get("new_regime", "")),
                nifty_price=float(flds.get("nifty_price", 0)),
                vix=float(flds.get("vix", 0)),
                trigger=str(flds.get("trigger", "")),
            ))

        # ---- Health ----
        elif ev == "system_heartbeat":
            data.heartbeat_count += 1
            if not data.first_heartbeat_ts:
                data.first_heartbeat_ts = ts
            data.last_heartbeat_ts = ts
            data.last_kill_switch_level = int(flds.get("kill_switch_level", 0))
            data.last_ws_connected = bool(flds.get("ws_connected", True))
            data.last_open_positions = int(flds.get("open_positions", 0))
            data.last_daily_pnl_pct = float(flds.get("daily_pnl_pct", 0.0))

        elif ev == "hard_exit_triggered":
            data.hard_exit_triggered = True
            data.hard_exit_ts = ts

        # ---- Warnings ----
        if level == "warning":
            data.warnings.append({"ts": ts, "event": ev, "fields": flds})


# ---------------------------------------------------------------------------
# Report formatters
# ---------------------------------------------------------------------------

def _hhmm(iso_ts: str) -> str:
    """Extract HH:MM from ISO timestamp. Falls back to original string on error."""
    try:
        return iso_ts.split("T")[1][:5]
    except Exception:
        return iso_ts


def fmt_session_header(data: SessionData) -> str:
    lines = [
        f"  Date:        {data.date or 'unknown'}",
        f"  Mode:        {data.mode.upper()}",
        f"  Instruments: {data.instruments}",
        f"  Start:       {_hhmm(data.start_ts) if data.start_ts else 'unknown'}",
        f"  End:         {_hhmm(data.end_ts) if data.end_ts else 'unknown'}",
    ]
    if data.hard_exit_triggered:
        lines.append(f"  Hard Exit:   YES at {_hhmm(data.hard_exit_ts)} IST")
    return "\n".join(lines)


def fmt_signal_table(data: SessionData, verbose: bool = False) -> str:
    if not data.signals:
        return "  (no signals)"

    header = (
        f"  {'#':>3}  "
        f"{'Time':<8}  "
        f"{'Symbol':<12}  "
        f"{'Dir':<5}  "
        f"{'Entry':>8}  "
        f"{'RSI':>5}  "
        f"{'VolR':>5}  "
        f"{'Status':<9}  "
        f"{'Details'}"
    )
    sep = "  " + "-" * 72
    rows = [header, sep]
    for i, sig in enumerate(data.signals, 1):
        if sig.status == "BLOCKED":
            gate_prefix = f"G{sig.gate} " if sig.gate else ""
            detail = f"{gate_prefix}{sig.block_reason}"
        elif sig.status == "DEDUP":
            detail = "duplicate suppressed"
        else:
            detail = f"stop={sig.stop:.2f}  target={sig.target:.2f}" if verbose else ""
        rows.append(
            f"  {i:>3}.  "
            f"{_hhmm(sig.ts):<8}  "
            f"{sig.symbol:<12}  "
            f"{sig.direction:<5}  "
            f"{sig.entry:>8.2f}  "
            f"{sig.rsi:>5.1f}  "
            f"{sig.volume_ratio:>5.2f}  "
            f"{sig.status:<9}  "
            f"{detail}"
        )
    return "\n".join(rows)


def fmt_trade_table(data: SessionData) -> str:
    if not data.trades:
        return "  (no trades)"

    header = (
        f"  {'#':>3}  "
        f"{'Symbol':<12}  "
        f"{'Dir':<5}  "
        f"{'Entry':>8}  "
        f"{'Qty':>5}  "
        f"{'Stop':>8}  "
        f"{'Target':>8}  "
        f"{'Open':>5}  "
        f"{'Status':<7}  "
        f"{'Exit':>8}  "
        f"{'P&L':>8}"
    )
    sep = "  " + "-" * 84
    rows = [header, sep]
    for i, trade in enumerate(data.trades, 1):
        pnl_str = f"+{trade.pnl_rs:.0f}" if (trade.pnl_rs and trade.pnl_rs >= 0) else (
            f"{trade.pnl_rs:.0f}" if trade.pnl_rs is not None else "—"
        )
        exit_str = f"{trade.exit_price:.2f}" if trade.exit_price else "—"
        rows.append(
            f"  {i:>3}.  "
            f"{trade.symbol:<12}  "
            f"{trade.direction:<5}  "
            f"{trade.entry_price:>8.2f}  "
            f"{trade.qty:>5}  "
            f"{trade.stop_loss:>8.2f}  "
            f"{trade.target:>8.2f}  "
            f"{_hhmm(trade.opened_at):>5}  "
            f"{trade.status:<7}  "
            f"{exit_str:>8}  "
            f"{pnl_str:>8}"
        )
    return "\n".join(rows)


def fmt_pnl_summary(data: SessionData) -> str:
    closed = [t for t in data.trades if t.exit_price is not None]
    open_trades = [t for t in data.trades if t.exit_price is None]
    wins = [t for t in closed if t.pnl_rs and t.pnl_rs > 0]
    losses = [t for t in closed if t.pnl_rs is not None and t.pnl_rs <= 0]
    net_pnl = sum(t.pnl_rs for t in closed if t.pnl_rs is not None)

    accepted = sum(1 for s in data.signals if s.status == "ACCEPTED")
    blocked = sum(1 for s in data.signals if s.status == "BLOCKED")
    dedup = sum(1 for s in data.signals if s.status == "DEDUP")

    lines = [
        f"  Positions:    {len(data.trades)} total  ({len(open_trades)} open, {len(closed)} closed)",
    ]
    if closed:
        win_rate = len(wins) / len(closed) * 100 if closed else 0.0
        lines.extend([
            f"  Result:       {len(wins)} wins, {len(losses)} losses  ({win_rate:.1f}% win rate)",
            f"  Net P&L:      ₹{net_pnl:+.2f}",
        ])
    else:
        lines.append("  Net P&L:      N/A (no closed positions — hard exit positions counted at EOD)")

    lines.extend([
        f"  Session PnL%: {data.last_daily_pnl_pct * 100:.3f}%",
        "",
        f"  Signals:      {len(data.signals)} total",
        f"  Accepted:     {accepted}",
        f"  Blocked:      {blocked}",
    ])
    if dedup:
        lines.append(f"  Dedup:        {dedup}")
    return "\n".join(lines)


def fmt_regime_timeline(data: SessionData) -> str:
    if not data.regime_events:
        return "  (no regime events)"

    header = (
        f"  {'Time':<8}  "
        f"{'Event':<12}  "
        f"{'From':<18}  "
        f"{'To':<18}  "
        f"{'Nifty':>9}  "
        f"{'VIX':>5}"
    )
    sep = "  " + "-" * 76
    rows = [header, sep]
    for ev in data.regime_events:
        from_str = ev.old_regime or "—"
        rows.append(
            f"  {_hhmm(ev.ts):<8}  "
            f"{ev.event_type:<12}  "
            f"{from_str:<18}  "
            f"{ev.new_regime:<18}  "
            f"{ev.nifty_price:>9.2f}  "
            f"{ev.vix:>5.2f}"
        )
    return "\n".join(rows)


def fmt_system_health(data: SessionData) -> str:
    lines = [
        f"  Heartbeats:   {data.heartbeat_count}",
        f"  First / Last: {_hhmm(data.first_heartbeat_ts) if data.first_heartbeat_ts else '—'}"
        f"  →  {_hhmm(data.last_heartbeat_ts) if data.last_heartbeat_ts else '—'}",
        f"  WS:           {'Connected' if data.last_ws_connected else 'DISCONNECTED'}",
        f"  Kill Switch:  Level {data.last_kill_switch_level}",
        f"  Open Pos:     {data.last_open_positions} (at last heartbeat)",
        f"  Warnings:     {len(data.warnings)}",
    ]
    if data.warnings:
        lines.append("")
        lines.append("  Warning log:")
        for w in data.warnings[:10]:
            lines.append(f"    {_hhmm(w['ts'])}  {w['event']}")
        if len(data.warnings) > 10:
            lines.append(f"    ... and {len(data.warnings) - 10} more")
    return "\n".join(lines)


def print_report(data: SessionData, verbose: bool = False) -> None:
    BAR = "═" * 72
    DIV = "─" * 72
    print(f"\n{BAR}")
    print("  TradeOS Session Report")
    print(BAR)
    print(fmt_session_header(data))

    sections = [
        ("2. Signals", fmt_signal_table(data, verbose=verbose)),
        ("3. Trades", fmt_trade_table(data)),
        ("4. P&L Summary", fmt_pnl_summary(data)),
        ("5. Regime Timeline", fmt_regime_timeline(data)),
        ("6. System Health", fmt_system_health(data)),
    ]
    for title, content in sections:
        print(f"\n{DIV}")
        print(f"  {title}")
        print(DIV)
        print(content)

    print(f"\n{BAR}\n")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _signals_rows(data: SessionData) -> tuple:
    headers = [
        "ts", "symbol", "direction", "entry", "stop", "target",
        "rsi", "volume_ratio", "status", "gate", "block_reason",
    ]
    rows = []
    for sig in data.signals:
        rows.append([
            sig.ts, sig.symbol, sig.direction,
            sig.entry, sig.stop, sig.target,
            sig.rsi, sig.volume_ratio,
            sig.status, sig.gate if sig.gate is not None else "",
            sig.block_reason,
        ])
    return headers, rows


def _trades_rows(data: SessionData) -> tuple:
    headers = [
        "symbol", "direction", "entry_price", "qty", "stop_loss", "target",
        "opened_at", "status", "exit_price", "exit_time", "exit_reason", "pnl_rs",
    ]
    rows = []
    for trade in data.trades:
        rows.append([
            trade.symbol, trade.direction,
            trade.entry_price, trade.qty,
            trade.stop_loss, trade.target,
            trade.opened_at, trade.status,
            trade.exit_price if trade.exit_price is not None else "",
            trade.exit_time or "",
            trade.exit_reason,
            trade.pnl_rs if trade.pnl_rs is not None else "",
        ])
    return headers, rows


def export_csv(data: SessionData, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    prefix = data.date or "session"
    for name, (headers, rows) in [
        ("signals", _signals_rows(data)),
        ("trades", _trades_rows(data)),
    ]:
        path = os.path.join(out_dir, f"{prefix}_{name}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(headers)
            w.writerows(rows)
        print(f"  Wrote: {path}")


def export_xlsx(data: SessionData, out_dir: str) -> None:
    try:
        import openpyxl
    except ImportError:
        print(
            "  ERROR: openpyxl not installed — run: pip install openpyxl",
            file=sys.stderr,
        )
        return
    os.makedirs(out_dir, exist_ok=True)
    prefix = data.date or "session"
    path = os.path.join(out_dir, f"{prefix}_report.xlsx")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, (headers, rows) in [
        ("Signals", _signals_rows(data)),
        ("Trades", _trades_rows(data)),
    ]:
        ws = wb.create_sheet(name)
        ws.append(headers)
        for row in rows:
            ws.append(row)
    wb.save(path)
    print(f"  Wrote: {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="TradeOS session log parser and report generator.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("logfile", help="Path to session log file")
    ap.add_argument(
        "--export",
        choices=["csv", "xlsx", "all"],
        help="Export data (csv, xlsx, or all)",
    )
    ap.add_argument(
        "--out-dir",
        default="reports",
        help="Output directory for exports (default: reports/)",
    )
    ap.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show extra detail in signal table (stop/target columns)",
    )
    args = ap.parse_args()

    if not os.path.exists(args.logfile):
        print(f"ERROR: log file not found: {args.logfile}", file=sys.stderr)
        sys.exit(1)

    parser = SessionParser()
    data = parser.parse(args.logfile)
    print_report(data, verbose=args.verbose)

    if args.export in ("csv", "all"):
        print("  Exporting CSV...")
        export_csv(data, args.out_dir)
    if args.export in ("xlsx", "all"):
        print("  Exporting XLSX...")
        export_xlsx(data, args.out_dir)


if __name__ == "__main__":
    main()
