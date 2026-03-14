#!/usr/bin/env python3
"""
TradeOS — Session Report Tool

Parses structlog session logs or queries the DB, prints a terminal report.
Supports optional CSV and Excel export.

Three modes:
  1. Log (default): parse a log file
  2. DB:            query PostgreSQL directly
  3. Verify:        cross-check log vs DB

Usage:
    # Log mode (default — backward compatible)
    python tools/session_report.py logs/tradeos/tradeos_2026-03-16.log
    python tools/session_report.py logs/tradeos/tradeos_2026-03-16.log --export csv

    # DB mode (no log file needed)
    python tools/session_report.py --source db --date 2026-03-16
    python tools/session_report.py --source db --date 2026-03-16 --export xlsx

    # Verify mode (cross-check log vs DB)
    python tools/session_report.py --verify logs/tradeos/tradeos_2026-03-16.log
    python tools/session_report.py --verify logs/tradeos/tradeos_2026-03-16.log --date 2026-03-16
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
    charges: float = 0.0
    net_pnl: Optional[float] = None

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


@dataclass
class SessionReport:
    """Normalized report from either log or DB source."""
    source: str              # "log" or "db"
    session_date: str        # YYYY-MM-DD
    signals: list = field(default_factory=list)    # [SignalRecord]
    trades: list = field(default_factory=list)     # [TradeRecord]
    total_signals: int = 0
    signals_accepted: int = 0
    signals_rejected: int = 0
    total_trades: int = 0
    trades_won: int = 0
    trades_lost: int = 0
    gross_pnl: float = 0.0
    total_charges: float = 0.0
    net_pnl: float = 0.0


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
# Log → SessionReport converter
# ---------------------------------------------------------------------------

def generate_log_report(data: SessionData) -> SessionReport:
    """Convert parsed SessionData into a normalized SessionReport."""
    closed = [t for t in data.trades if t.exit_price is not None]
    wins = [t for t in closed if t.pnl_rs and t.pnl_rs > 0]
    losses = [t for t in closed if t.pnl_rs is not None and t.pnl_rs <= 0]
    gross_pnl = sum(t.pnl_rs for t in closed if t.pnl_rs is not None)
    accepted = sum(1 for s in data.signals if s.status == "ACCEPTED")
    blocked = sum(1 for s in data.signals if s.status in ("BLOCKED", "DEDUP"))

    return SessionReport(
        source="log",
        session_date=data.date or "unknown",
        signals=data.signals,
        trades=data.trades,
        total_signals=len(data.signals),
        signals_accepted=accepted,
        signals_rejected=blocked,
        total_trades=len(data.trades),
        trades_won=len(wins),
        trades_lost=len(losses),
        gross_pnl=gross_pnl,
        total_charges=0.0,
        net_pnl=gross_pnl,
    )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_nested(d: dict, dotted_key: str) -> object:
    """Traverse a nested dict by dot-separated key path."""
    val: object = d
    for part in dotted_key.split("."):
        if not isinstance(val, dict):
            return None
        val = val.get(part)
    return val


def _load_dsn() -> str:
    """Load DB DSN from config files, matching main.py pattern."""
    import yaml
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    settings_path = os.path.join(root, "config", "settings.yaml")
    secrets_path = os.path.join(root, "config", "secrets.yaml")
    with open(settings_path) as f:
        config = yaml.safe_load(f) or {}
    try:
        with open(secrets_path) as f:
            secrets = yaml.safe_load(f) or {}
    except FileNotFoundError:
        secrets = {}
    return str(
        _get_nested(config, "database.dsn")
        or _get_nested(config, "db.dsn")
        or _get_nested(secrets, "database.dsn")
        or ""
    )


_SIGNAL_STATUS_MAP = {
    "FILLED": "ACCEPTED",
    "REJECTED": "BLOCKED",
    "KILL_SWITCHED": "BLOCKED",
    "IGNORED": "BLOCKED",
    "PENDING": "PENDING",
}


def _map_signal_status(db_status: str) -> str:
    """Map DB signal status to report display status."""
    return _SIGNAL_STATUS_MAP.get(db_status, db_status)


# ---------------------------------------------------------------------------
# DB → SessionReport
# ---------------------------------------------------------------------------

async def _generate_db_report_async(dsn: str, session_date: str) -> SessionReport:
    """Query DB for signals, trades, and session summary."""
    import asyncpg
    from datetime import date as date_type

    dt = date_type.fromisoformat(session_date)
    conn = await asyncpg.connect(dsn)
    try:
        sig_rows = await conn.fetch(
            "SELECT symbol, direction, status, reject_reason, "
            "signal_time, theoretical_entry, stop_loss, target, "
            "rsi, volume_ratio "
            "FROM signals WHERE session_date = $1 ORDER BY signal_time",
            dt,
        )
        trade_rows = await conn.fetch(
            "SELECT t.symbol, t.direction, t.qty, "
            "t.actual_entry, t.actual_exit, "
            "t.entry_time, t.exit_time, t.exit_reason, "
            "t.gross_pnl, t.charges, t.net_pnl, "
            "s.stop_loss AS signal_stop, s.target AS signal_target "
            "FROM trades t "
            "LEFT JOIN signals s ON t.signal_id = s.id "
            "WHERE t.session_date = $1 ORDER BY t.entry_time",
            dt,
        )
        session_row = await conn.fetchrow(
            "SELECT * FROM sessions WHERE session_date = $1",
            dt,
        )
    finally:
        await conn.close()

    signals = []
    for r in sig_rows:
        signals.append(SignalRecord(
            ts=str(r["signal_time"]),
            symbol=r["symbol"],
            direction=r["direction"],
            entry=float(r["theoretical_entry"]),
            stop=float(r["stop_loss"]),
            target=float(r["target"]),
            rsi=float(r["rsi"]),
            volume_ratio=float(r["volume_ratio"]),
            status=_map_signal_status(r["status"]),
            block_reason=r["reject_reason"] or "",
        ))

    trades = []
    for r in trade_rows:
        gross = float(r["gross_pnl"]) if r["gross_pnl"] is not None else None
        charges = float(r["charges"]) if r["charges"] is not None else 0.0
        net = float(r["net_pnl"]) if r["net_pnl"] is not None else None
        trades.append(TradeRecord(
            symbol=r["symbol"],
            direction=r["direction"],
            entry_price=float(r["actual_entry"]),
            qty=r["qty"],
            stop_loss=float(r["signal_stop"]) if r["signal_stop"] is not None else 0.0,
            target=float(r["signal_target"]) if r["signal_target"] is not None else 0.0,
            opened_at=str(r["entry_time"]) if r["entry_time"] else "",
            exit_price=float(r["actual_exit"]) if r["actual_exit"] is not None else None,
            exit_time=str(r["exit_time"]) if r["exit_time"] else None,
            exit_reason=r["exit_reason"] or "",
            pnl_rs=gross,
            charges=charges,
            net_pnl=net,
        ))

    if session_row:
        return SessionReport(
            source="db",
            session_date=session_date,
            signals=signals,
            trades=trades,
            total_signals=session_row["signals_total"],
            signals_accepted=session_row["signals_accepted"],
            signals_rejected=session_row["signals_rejected"],
            total_trades=session_row["trades_total"],
            trades_won=session_row["trades_won"],
            trades_lost=session_row["trades_lost"],
            gross_pnl=float(session_row["gross_pnl"]),
            total_charges=float(session_row["total_charges"]),
            net_pnl=float(session_row["net_pnl"]),
        )

    # No sessions row — compute from individual records
    closed = [t for t in trades if t.exit_price is not None]
    sig_accepted = sum(1 for s in signals if s.status == "ACCEPTED")
    sig_rejected = sum(1 for s in signals if s.status == "BLOCKED")
    wins = sum(1 for t in closed if t.pnl_rs and t.pnl_rs > 0)
    losses = sum(1 for t in closed if t.pnl_rs is not None and t.pnl_rs <= 0)
    gross = sum(t.pnl_rs for t in closed if t.pnl_rs is not None)
    tot_charges = sum(t.charges for t in trades)

    return SessionReport(
        source="db",
        session_date=session_date,
        signals=signals,
        trades=trades,
        total_signals=len(signals),
        signals_accepted=sig_accepted,
        signals_rejected=sig_rejected,
        total_trades=len(trades),
        trades_won=wins,
        trades_lost=losses,
        gross_pnl=gross,
        total_charges=tot_charges,
        net_pnl=gross - tot_charges,
    )


def generate_db_report(dsn: str, session_date: str) -> SessionReport:
    """Sync wrapper for async DB query."""
    import asyncio
    return asyncio.run(_generate_db_report_async(dsn, session_date))


# ---------------------------------------------------------------------------
# SessionReport printer (reuses existing formatters via SessionData shim)
# ---------------------------------------------------------------------------

def _fmt_pnl_from_report(report: SessionReport) -> str:
    """P&L summary from SessionReport — shows charges when available."""
    lines = [
        f"  Trades:       {report.total_trades} total",
    ]
    if report.trades_won or report.trades_lost:
        total_closed = report.trades_won + report.trades_lost
        win_rate = report.trades_won / total_closed * 100 if total_closed else 0.0
        lines.append(f"  Result:       {report.trades_won} wins, {report.trades_lost} losses  ({win_rate:.1f}% win rate)")

    lines.append(f"  Gross P&L:    \u20b9{report.gross_pnl:+.2f}")
    if report.total_charges > 0:
        lines.append(f"  Charges:      \u20b9{report.total_charges:-.2f}")
    lines.append(f"  Net P&L:      \u20b9{report.net_pnl:+.2f}")
    lines.extend([
        "",
        f"  Signals:      {report.total_signals} total",
        f"  Accepted:     {report.signals_accepted}",
        f"  Rejected:     {report.signals_rejected}",
    ])
    return "\n".join(lines)


def print_session_report(report: SessionReport, verbose: bool = False) -> None:
    """Print report from SessionReport (works for both log and DB sources)."""
    shim = SessionData(
        date=report.session_date,
        signals=report.signals,
        trades=report.trades,
    )
    BAR = "\u2550" * 72
    DIV = "\u2500" * 72
    print(f"\n{BAR}")
    print(f"  TradeOS Session Report  [Source: {report.source.upper()}]")
    print(BAR)
    print(f"  Date:        {report.session_date}")

    sections = [
        ("2. Signals", fmt_signal_table(shim, verbose=verbose)),
        ("3. Trades", fmt_trade_table(shim)),
        ("4. P&L Summary", _fmt_pnl_from_report(report)),
    ]
    for title, content in sections:
        print(f"\n{DIV}")
        print(f"  {title}")
        print(DIV)
        print(content)

    print(f"\n{BAR}\n")


# ---------------------------------------------------------------------------
# Verify mode — cross-check log vs DB
# ---------------------------------------------------------------------------

@dataclass
class VerifyResult:
    field: str
    log_value: object
    db_value: object
    match: bool = True


def verify_reports(log_report: SessionReport, db_report: SessionReport) -> list[VerifyResult]:
    """Compare log-based and DB-based SessionReport objects field by field."""
    results: list[VerifyResult] = []

    # Summary-level exact checks
    results.append(VerifyResult("signals", log_report.total_signals, db_report.total_signals,
                                log_report.total_signals == db_report.total_signals))
    results.append(VerifyResult("trades", log_report.total_trades, db_report.total_trades,
                                log_report.total_trades == db_report.total_trades))

    # Per-trade comparison — match by symbol+direction
    log_trades = sorted(log_report.trades, key=lambda t: (t.symbol, t.direction))
    db_trades = sorted(db_report.trades, key=lambda t: (t.symbol, t.direction))
    min_len = min(len(log_trades), len(db_trades))

    for i in range(min_len):
        lt = log_trades[i]
        dt = db_trades[i]
        prefix = f"Trade #{i+1} {lt.symbol} {lt.direction}"

        results.append(VerifyResult(f"{prefix} entry", lt.entry_price, dt.entry_price,
                                    abs(lt.entry_price - dt.entry_price) <= 0.01))
        if lt.exit_price is not None and dt.exit_price is not None:
            results.append(VerifyResult(f"{prefix} exit", lt.exit_price, dt.exit_price,
                                        abs(lt.exit_price - dt.exit_price) <= 0.01))
        results.append(VerifyResult(f"{prefix} qty", lt.qty, dt.qty,
                                    lt.qty == dt.qty))
        if lt.pnl_rs is not None and dt.pnl_rs is not None:
            results.append(VerifyResult(f"{prefix} gross", lt.pnl_rs, dt.pnl_rs,
                                        abs(lt.pnl_rs - dt.pnl_rs) <= 1.0))
        results.append(VerifyResult(f"{prefix} reason", lt.exit_reason, dt.exit_reason,
                                    lt.exit_reason == dt.exit_reason))

    # Extra trades mismatch
    if len(log_trades) != len(db_trades):
        results.append(VerifyResult("trade_count", len(log_trades), len(db_trades), False))

    # Session P&L totals (tolerance ±2.0)
    results.append(VerifyResult("session_pnl", log_report.net_pnl, db_report.net_pnl,
                                abs(log_report.net_pnl - db_report.net_pnl) <= 2.0))

    return results


def _extract_date_from_filename(filepath: str) -> Optional[str]:
    """Extract YYYY-MM-DD from filename like tradeos_2026-03-16.log."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(filepath))
    return m.group(1) if m else None


def print_verify_results(results: list[VerifyResult], session_date: str) -> int:
    """Print verification output in the specified format. Returns exit code (0=pass, 1=fail)."""
    print(f"\n=== VERIFICATION: {session_date} ===")

    for r in results:
        icon = "\u2705" if r.match else "\u274c MISMATCH"
        if r.field in ("signals", "trades"):
            print(f"{r.field.capitalize():10s} LOG={r.log_value}  DB={r.db_value}  {icon}")
        elif r.field == "trade_count":
            print(f"Trade count: LOG={r.log_value}  DB={r.db_value}  {icon}")
        elif r.field == "session_pnl":
            log_v = f"{r.log_value:.2f}" if isinstance(r.log_value, float) else str(r.log_value)
            db_v = f"{r.db_value:.2f}" if isinstance(r.db_value, float) else str(r.db_value)
            print(f"Session P&L: LOG={log_v}  DB={db_v}  {icon}")
        elif r.field.startswith("Trade #"):
            # Per-trade fields
            field_parts = r.field.rsplit(" ", 1)
            trade_label = field_parts[0]
            field_name = field_parts[1] if len(field_parts) > 1 else ""
            # Print trade header on first field
            if field_name == "entry":
                print(f"{trade_label}:")
            label = field_name.capitalize().replace("_", " ")
            if isinstance(r.log_value, float):
                print(f"  {label + ':':<10s} LOG={r.log_value:<12.2f} DB={r.db_value:<12.2f} {icon}")
            else:
                print(f"  {label + ':':<10s} LOG={str(r.log_value):<12s} DB={str(r.db_value):<12s} {icon}")

    mismatches = [r for r in results if not r.match]
    if mismatches:
        print(f"\nRESULT: \u274c {len(mismatches)} MISMATCH(ES) found \u2014 log and DB are inconsistent.")
        return 1
    else:
        print(f"\nRESULT: \u2705 ALL MATCH \u2014 log and DB are consistent.")
        return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _do_export(data: SessionData, args) -> None:
    """Run CSV/XLSX export if requested."""
    if args.export in ("csv", "all"):
        print("  Exporting CSV...")
        export_csv(data, args.out_dir)
    if args.export in ("xlsx", "all"):
        print("  Exporting XLSX...")
        export_xlsx(data, args.out_dir)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="TradeOS session report — log parser, DB query, or verify.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "logfile", nargs="?", default=None,
        help="Path to session log file (for log mode)",
    )
    ap.add_argument(
        "--source",
        choices=["log", "db"],
        default="log",
        help="Data source: log (default) or db",
    )
    ap.add_argument(
        "--date",
        help="Session date YYYY-MM-DD (required for DB mode, optional for verify)",
    )
    ap.add_argument(
        "--verify",
        metavar="LOGFILE",
        help="Cross-check log file vs DB",
    )
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

    # --- Mode 3: Verify (log vs DB) ---
    if args.verify:
        if not os.path.exists(args.verify):
            print(f"ERROR: log file not found: {args.verify}", file=sys.stderr)
            sys.exit(1)

        parser = SessionParser()
        data = parser.parse(args.verify)
        log_report = generate_log_report(data)

        session_date = args.date or _extract_date_from_filename(args.verify) or data.date
        if not session_date:
            print("ERROR: could not determine session date. Use --date YYYY-MM-DD", file=sys.stderr)
            sys.exit(1)

        dsn = _load_dsn()
        if not dsn:
            print("ERROR: no database.dsn found in config", file=sys.stderr)
            sys.exit(1)

        db_report = generate_db_report(dsn, session_date)
        results = verify_reports(log_report, db_report)
        exit_code = print_verify_results(results, session_date)

        if args.export:
            shim = SessionData(date=session_date, signals=log_report.signals, trades=log_report.trades)
            _do_export(shim, args)

        sys.exit(exit_code)

    # --- Mode 2: DB report ---
    if args.source == "db":
        if not args.date:
            print("ERROR: --source db requires --date YYYY-MM-DD", file=sys.stderr)
            sys.exit(1)

        dsn = _load_dsn()
        if not dsn:
            print("ERROR: no database.dsn found in config", file=sys.stderr)
            sys.exit(1)

        report = generate_db_report(dsn, args.date)
        print_session_report(report, verbose=args.verbose)

        if args.export:
            shim = SessionData(date=report.session_date, signals=report.signals, trades=report.trades)
            _do_export(shim, args)
        return

    # --- Mode 1: Log report (default — backward compatible) ---
    if not args.logfile:
        print("ERROR: logfile argument is required for log mode", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(args.logfile):
        print(f"ERROR: log file not found: {args.logfile}", file=sys.stderr)
        sys.exit(1)

    parser = SessionParser()
    data = parser.parse(args.logfile)
    print_report(data, verbose=args.verbose)

    _do_export(data, args)


if __name__ == "__main__":
    main()
