"""
TradeOS — Risk Gate (pre-signal validation pipeline, D6 signal_processor gates)

7 gates run in strict order. First failure stops evaluation and returns the reason.
Returns (allowed: bool, reason: str).

Gate sequence:
  Gate 0 — Mode assertion       (paper mode hard check — crashes on misconfiguration)
  Gate 1 — Kill switch          (D1 — no trading at level 1, 2, or 3)
  Gate 2 — Recon in progress    (D7 — no signals during reconciliation)
  Gate 3 — Instrument locked    (D7 — instrument under reconciliation lock)
  Gate 4 — Max open positions   (D6 — max 3 concurrent positions)
  Gate 5 — Hard exit time       (15:00 IST — no new signals after this)
  Gate 6 — Duplicate signal     (already have a live position in same direction)

Kill switch integration: accepts an optional kill_switch object (D1 KillSwitch).
If not provided, falls back to shared_state["kill_switch_level"] as a read cache.
"""
from __future__ import annotations

import structlog
from datetime import datetime, time
from typing import Optional, Protocol, runtime_checkable

import pytz

from strategy_engine.signal_generator import Signal


@runtime_checkable
class KillSwitchProtocol(Protocol):
    """Structural protocol for D1 KillSwitch — avoids circular import."""

    def is_trading_allowed(self) -> bool: ...

log = structlog.get_logger()

IST = pytz.timezone("Asia/Kolkata")
HARD_EXIT_TIME: time = time(15, 0)


class RiskGate:
    """
    Pre-signal validation pipeline enforcing all D1/D6/D7 trading gates.

    All 7 gates execute in strict order. First failure = signal dropped.
    Gate 0 asserts paper mode — it crashes hard on misconfiguration by design.
    """

    def __init__(self, kill_switch: Optional[KillSwitchProtocol] = None) -> None:
        """
        Args:
            kill_switch: Optional KillSwitch instance (D1).
                         If None, falls back to shared_state["kill_switch_level"].
        """
        self._kill_switch = kill_switch

    def check(
        self,
        signal: Signal,
        shared_state: dict,
        config: dict,
    ) -> tuple[bool, str]:
        """
        Run the signal through all 7 gates in order.

        Args:
            signal: Candidate Signal from SignalGenerator.
            shared_state: D6 shared state dict (canonical keys).
            config: Loaded settings.yaml dict.

        Returns:
            (True, "OK") if all gates pass.
            (False, reason_string) at the first gate failure.
        """
        # Gate 0: mode assertion — crashes if misconfigured
        mode = config.get("system", {}).get("mode", "")
        assert mode == "paper", (
            f"RiskGate: system.mode must be 'paper', got '{mode}'. "
            "Set system.mode: paper in config/settings.yaml."
        )

        # Gate 1: kill switch (D1)
        ks_blocked = False
        if self._kill_switch is not None:
            # Use KillSwitch.is_trading_allowed() — authoritative D1 source
            ks_blocked = not self._kill_switch.is_trading_allowed()
        else:
            # Fallback: read cache in shared_state (may lag by one asyncio cycle)
            ks_blocked = shared_state.get("kill_switch_level", 0) > 0

        if ks_blocked:
            level = shared_state.get("kill_switch_level", 0)
            reason = f"KILL_SWITCH_LEVEL_{level}"
            log.debug("risk_gate_blocked", gate=1, reason=reason, symbol=signal.symbol)
            return False, reason

        # Gate 2: reconciliation in progress (D7)
        if shared_state.get("recon_in_progress", False):
            log.debug(
                "risk_gate_blocked", gate=2, reason="RECON_IN_PROGRESS",
                symbol=signal.symbol,
            )
            return False, "RECON_IN_PROGRESS"

        # Gate 3: instrument locked (D7)
        if signal.symbol in shared_state.get("locked_instruments", set()):
            log.debug(
                "risk_gate_blocked", gate=3, reason="INSTRUMENT_LOCKED",
                symbol=signal.symbol,
            )
            return False, "INSTRUMENT_LOCKED"

        # Gate 4: max open positions (D6)
        max_positions = config.get("risk", {}).get("max_open_positions", 3)
        open_count = len(shared_state.get("open_positions", {}))
        if open_count >= max_positions:
            log.debug(
                "risk_gate_blocked", gate=4, reason="MAX_POSITIONS_REACHED",
                symbol=signal.symbol, open_positions=open_count,
            )
            return False, "MAX_POSITIONS_REACHED"

        # Gate 5: hard exit time — no new signals at or after 15:00 IST
        now_ist = datetime.now(IST)
        if now_ist.time() >= HARD_EXIT_TIME:
            log.debug(
                "risk_gate_blocked", gate=5, reason="HARD_EXIT_TIME_REACHED",
                symbol=signal.symbol,
            )
            return False, "HARD_EXIT_TIME_REACHED"

        # Gate 6: duplicate signal — same direction already in open positions
        open_positions = shared_state.get("open_positions", {})
        if signal.symbol in open_positions:
            pos = open_positions[signal.symbol]
            pos_side = pos.get("side", "")
            is_dup = (
                (signal.direction == "LONG" and pos_side == "BUY")
                or (signal.direction == "SHORT" and pos_side == "SELL")
            )
            if is_dup:
                log.debug(
                    "risk_gate_blocked", gate=6, reason="DUPLICATE_SIGNAL",
                    symbol=signal.symbol, direction=signal.direction,
                )
                return False, "DUPLICATE_SIGNAL"

        return True, "OK"
