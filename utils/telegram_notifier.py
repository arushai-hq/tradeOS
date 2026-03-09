"""
TradeOS — Rich Telegram Notifier

Wraps send_telegram() with:
  - Per-event-type enable/disable flags (config/telegram_alerts.yaml)
  - Hot-reload: config re-read every 60 seconds, no restart needed
  - Rich HTML-formatted messages with monospace tables via <pre> tags
  - Fire-and-forget via asyncio.create_task() — never blocks trading loop

Event types:
  signal_generated  → notify_signal_accepted / notify_signal_rejected
  position_opened   → notify_position_opened   (order_filled)
  stop_hit          → notify_position_closed with STOP_HIT reason
  target_hit        → notify_position_closed with TARGET_HIT reason
  hard_exit         → notify_hard_exit          (batch position summary)
  heartbeat_summary → notify_heartbeat          (periodic table)

Usage:
    notifier = TelegramNotifier("config/telegram_alerts.yaml", shared_state, secrets)
    notifier.notify_signal_accepted("HCLTECH", "LONG", 1370.60, 1345.20, 1421.40,
                                    59.9, 1.96, "bear_trend")
"""
from __future__ import annotations

import asyncio
import html
import time
from datetime import datetime
from typing import Optional

import pytz
import structlog
import yaml

from utils.telegram import send_telegram

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_hold_time(minutes: float) -> str:
    """Format hold duration minutes as human-readable string."""
    minutes = max(0.0, minutes)
    if minutes < 60:
        return f"{int(minutes)} min"
    h = int(minutes // 60)
    m = int(minutes % 60)
    return f"{h}h {m}min" if m else f"{h}h"


def _unrealized_pnl(open_positions: dict, tick_prices: dict) -> float:
    """Compute total unrealized P&L across all open positions."""
    total = 0.0
    for symbol, pos in open_positions.items():
        current = tick_prices.get(symbol)
        if current is None:
            continue
        entry = float(pos.get("entry_price", 0.0))
        qty = int(pos.get("qty", 0))
        direction = pos.get("direction", "LONG")
        if direction == "LONG":
            total += (float(current) - entry) * qty
        else:
            total += (entry - float(current)) * qty
    return total


# ---------------------------------------------------------------------------
# TelegramNotifier
# ---------------------------------------------------------------------------

class TelegramNotifier:
    """
    Rich Telegram notifications with hot-reloadable per-event config.

    All notify_*() methods are synchronous fire-and-forget — they schedule
    send_telegram() as an asyncio.Task so the calling coroutine is never
    blocked by a network call.

    Args:
        config_path:  Path to telegram_alerts.yaml (absolute or relative to cwd).
        shared_state: D6 shared state dict.
        secrets:      Loaded secrets.yaml dict (for bot_token + chat_id).
    """

    _CACHE_TTL: float = 60.0  # seconds before re-reading config file

    def __init__(
        self,
        config_path: str,
        shared_state: dict,
        secrets: dict,
    ) -> None:
        self._config_path = config_path
        self._shared_state = shared_state
        self._secrets = secrets
        self._alert_config: dict = {}
        self._cache_loaded_at: Optional[float] = None

    # ------------------------------------------------------------------
    # Config hot-reload
    # ------------------------------------------------------------------

    def _load_alert_config(self) -> dict:
        """
        Return alert config dict. Re-reads file if cache is >60 s old.
        On read error, returns last known good config (or empty dict).
        """
        now = time.monotonic()
        if (
            self._cache_loaded_at is None
            or (now - self._cache_loaded_at) >= self._CACHE_TTL
        ):
            try:
                with open(self._config_path) as f:
                    raw = yaml.safe_load(f) or {}
                self._alert_config = raw.get("telegram_alerts", {})
                self._cache_loaded_at = now
            except Exception as exc:
                log.warning(
                    "telegram_alert_config_load_failed",
                    path=self._config_path,
                    error=str(exc),
                )
        return self._alert_config

    def _is_enabled(self, key: str) -> bool:
        """Return True if event type is enabled (default: True if key absent)."""
        return bool(self._load_alert_config().get(key, True))

    def heartbeat_interval_cycles(self) -> int:
        """
        Return number of 30-second heartbeat cycles between Telegram summaries.
        heartbeat_interval_min: 30  → 60 cycles
        heartbeat_interval_min: 60  → 120 cycles
        """
        cfg = self._load_alert_config()
        interval_min = int(cfg.get("heartbeat_interval_min", 30))
        return max(1, interval_min) * 2  # cycles × 30s = interval_min × 60s

    # ------------------------------------------------------------------
    # Internal send (fire-and-forget)
    # ------------------------------------------------------------------

    def _send(self, msg: str) -> None:
        """
        Schedule send_telegram() as a fire-and-forget asyncio.Task.

        Only runs when there is a running event loop (i.e., during trading).
        Silent no-op outside asyncio context (e.g., tests that don't run a loop).
        """
        try:
            asyncio.get_running_loop()
            asyncio.create_task(
                send_telegram(msg, self._shared_state, self._secrets, parse_mode="HTML")
            )
        except RuntimeError:
            pass  # No running loop — test context or pre-startup

    # ------------------------------------------------------------------
    # Public notify methods
    # ------------------------------------------------------------------

    def notify_signal_accepted(
        self,
        symbol: str,
        direction: str,
        entry: float,
        stop: float,
        target: float,
        rsi: float,
        vol_ratio: float,
        regime: str,
    ) -> None:
        if not self._is_enabled("signal_generated"):
            return
        self._send(self._fmt_signal_accepted(
            symbol, direction, entry, stop, target, rsi, vol_ratio, regime
        ))

    def notify_signal_rejected(
        self,
        symbol: str,
        direction: str,
        gate_name: str,
        gate_number: int,
        reason: str,
        rsi: float,
    ) -> None:
        if not self._is_enabled("signal_generated"):
            return
        self._send(self._fmt_signal_rejected(
            symbol, direction, gate_name, gate_number, reason, rsi
        ))

    def notify_position_opened(
        self,
        symbol: str,
        direction: str,
        fill_price: float,
        qty: int,
        stop_loss: float,
        target: float,
    ) -> None:
        if not self._is_enabled("position_opened"):
            return
        self._send(self._fmt_position_opened(
            symbol, direction, fill_price, qty, stop_loss, target
        ))

    def notify_position_closed(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        exit_reason: str,
        pnl_points: float,
        pnl_pct: float,
        hold_duration_minutes: float,
    ) -> None:
        """
        Route to stop/target notification based on exit_reason.
        HARD_EXIT_1500 and KILL_SWITCH are handled via notify_hard_exit (batch) — skipped here.
        """
        if exit_reason == "TARGET_HIT":
            if not self._is_enabled("target_hit"):
                return
            msg = self._fmt_target_hit(
                symbol, direction, entry_price, exit_price,
                pnl_points, pnl_pct, hold_duration_minutes
            )
        elif exit_reason == "STOP_HIT":
            if not self._is_enabled("stop_hit"):
                return
            msg = self._fmt_stop_hit(
                symbol, direction, entry_price, exit_price,
                pnl_points, pnl_pct, hold_duration_minutes
            )
        else:
            # HARD_EXIT_1500 / KILL_SWITCH — batch message sent separately
            return
        self._send(msg)

    def notify_hard_exit(
        self,
        positions_snapshot: dict,
        tick_prices: dict,
        session_pnl_rs: float,
    ) -> None:
        if not self._is_enabled("hard_exit"):
            return
        self._send(self._fmt_hard_exit(positions_snapshot, tick_prices, session_pnl_rs))

    def notify_heartbeat(self) -> None:
        if not self._is_enabled("heartbeat_summary"):
            return
        self._send(self._fmt_heartbeat())

    # ------------------------------------------------------------------
    # Message formatters
    # ------------------------------------------------------------------

    def _fmt_signal_accepted(
        self,
        symbol: str,
        direction: str,
        entry: float,
        stop: float,
        target: float,
        rsi: float,
        vol_ratio: float,
        regime: str,
    ) -> str:
        icon = "🟢" if direction == "LONG" else "🔴"
        risk_pts = abs(entry - stop)
        reward_pts = abs(target - entry)
        risk_pct = risk_pts / entry * 100 if entry else 0.0
        reward_pct = reward_pts / entry * 100 if entry else 0.0
        return (
            f"{icon} Signal: {direction} {html.escape(symbol)}\n"
            f"Entry: ₹{entry:.2f} | Stop: ₹{stop:.2f} | Target: ₹{target:.2f}\n"
            f"RSI: {rsi:.1f} | Vol Ratio: {vol_ratio:.2f}\n"
            f"Risk: ₹{risk_pts:.2f} ({risk_pct:.2f}%) | Reward: ₹{reward_pts:.2f} ({reward_pct:.2f}%)\n"
            f"Regime: {html.escape(regime)}"
        )

    def _fmt_signal_rejected(
        self,
        symbol: str,
        direction: str,
        gate_name: str,
        gate_number: int,
        reason: str,
        rsi: float,
    ) -> str:
        return (
            f"🔴 Signal Rejected: {direction} {html.escape(symbol)}\n"
            f"Reason: Gate {gate_number} — {html.escape(gate_name)} "
            f"({html.escape(reason)}) | RSI: {rsi:.1f}"
        )

    def _fmt_position_opened(
        self,
        symbol: str,
        direction: str,
        fill_price: float,
        qty: int,
        stop_loss: float,
        target: float,
    ) -> str:
        capital_at_risk = abs(fill_price - stop_loss) * qty
        return (
            f"📈 Position Opened: {direction} {html.escape(symbol)}\n"
            f"Fill: ₹{fill_price:.2f} | Qty: {qty}\n"
            f"Stop: ₹{stop_loss:.2f} | Target: ₹{target:.2f}\n"
            f"Capital at risk: ₹{capital_at_risk:,.0f}"
        )

    def _fmt_stop_hit(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        pnl_points: float,
        pnl_pct: float,
        hold_duration_minutes: float,
    ) -> str:
        sign = "+" if pnl_points >= 0 else ""
        return (
            f"🛑 Stop Hit: {direction} {html.escape(symbol)}\n"
            f"Entry: ₹{entry_price:.2f} → Exit: ₹{exit_price:.2f}\n"
            f"P&amp;L: {sign}₹{pnl_points:.2f} ({sign}{pnl_pct:.2f}%)\n"
            f"Hold time: {_fmt_hold_time(hold_duration_minutes)}"
        )

    def _fmt_target_hit(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        pnl_points: float,
        pnl_pct: float,
        hold_duration_minutes: float,
    ) -> str:
        sign = "+" if pnl_points >= 0 else ""
        return (
            f"🎯 Target Hit: {direction} {html.escape(symbol)}\n"
            f"Entry: ₹{entry_price:.2f} → Exit: ₹{exit_price:.2f}\n"
            f"P&amp;L: {sign}₹{pnl_points:.2f} ({sign}{pnl_pct:.2f}%)\n"
            f"Hold time: {_fmt_hold_time(hold_duration_minutes)}"
        )

    def _fmt_hard_exit(
        self,
        positions_snapshot: dict,
        tick_prices: dict,
        session_pnl_rs: float,
    ) -> str:
        count = len(positions_snapshot)
        rows = []
        total_unrealized = 0.0
        for sym, pos in positions_snapshot.items():
            direction = pos.get("direction", "LONG")
            entry = float(pos.get("entry_price", 0.0))
            current = float(tick_prices.get(sym, entry))
            qty = int(pos.get("qty", 0))
            pnl = (current - entry) * qty if direction == "LONG" else (entry - current) * qty
            total_unrealized += pnl
            sign = "+" if pnl >= 0 else ""
            rows.append(
                f"{sym:<10} {direction:<5} {entry:>8.2f}  {current:>8.2f}  {sign}₹{pnl:.0f}"
            )
        header = f"{'Symbol':<10} {'Dir':<5} {'Entry':>8}  {'Exit':>8}  {'P&L'}"
        sep = "-" * 48
        table = f"{header}\n{sep}\n" + ("\n".join(rows) if rows else "—")
        total = session_pnl_rs + total_unrealized
        sign = "+" if total >= 0 else ""
        return (
            f"⚠️ Hard Exit (15:00 IST)\n"
            f"Closing {count} position{'s' if count != 1 else ''}:\n"
            f"<pre>{table}</pre>\n"
            f"Session P&amp;L: {sign}₹{total:.0f}"
        )

    def _fmt_heartbeat(self) -> str:
        ts = datetime.now(IST).strftime("%H:%M IST")
        regime = self._shared_state.get("market_regime") or "unknown"
        open_pos = self._shared_state.get("open_positions", {})
        tick_prices = self._shared_state.get("last_tick_prices", {})
        pnl_rs = self._shared_state.get("daily_pnl_rs", 0.0)
        unrealized = _unrealized_pnl(open_pos, tick_prices)
        total_pnl = pnl_rs + unrealized
        pnl_sign = "+" if total_pnl >= 0 else ""
        accepted = self._shared_state.get("signals_generated_today", 0)
        rejected = self._shared_state.get("signals_rejected_today", 0)
        pos_count = len(open_pos)

        if pos_count == 0:
            table_str = "No open positions"
        else:
            header = f"{'Symbol':<10} {'Dir':<5} {'Entry':>8}  {'Current':>8}  {'Unrl P&L':>10}"
            sep = "-" * 52
            rows = []
            for sym, pos in open_pos.items():
                direction = pos.get("direction", "LONG")
                entry = float(pos.get("entry_price", 0.0))
                current = float(tick_prices.get(sym, entry))
                qty = int(pos.get("qty", 0))
                unrl = (
                    (current - entry) * qty
                    if direction == "LONG"
                    else (entry - current) * qty
                )
                unrl_sign = "+" if unrl >= 0 else ""
                rows.append(
                    f"{sym:<10} {direction:<5} {entry:>8.2f}  {current:>8.2f}  "
                    f"{unrl_sign}₹{unrl:.2f}"
                )
            table_str = f"{header}\n{sep}\n" + "\n".join(rows)

        return (
            f"💓 TradeOS — {ts}\n"
            f"Regime: {html.escape(regime)} | Positions: {pos_count} open\n"
            f"<pre>{table_str}</pre>\n"
            f"Session P&amp;L: {pnl_sign}₹{total_pnl:.0f}\n"
            f"Signals today: {accepted} accepted, {rejected} rejected"
        )
