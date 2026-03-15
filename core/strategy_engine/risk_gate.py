"""
TradeOS — Risk Gate (pre-signal validation pipeline, D6 signal_processor gates)

8 gates run in strict order. First failure stops evaluation and returns the reason.
Returns (allowed: bool, reason: str).

Gate sequence:
  Gate 0 — Mode assertion       (paper mode hard check — crashes on misconfiguration)
  Gate 1 — Kill switch          (D1 — no trading at level 1, 2, or 3)
  Gate 2 — Recon in progress    (D7 — no signals during reconciliation)
  Gate 3 — Instrument locked    (D7 — instrument under reconciliation lock)
  Gate 4 — Max open positions   (D6 — max 3 concurrent positions)
  Gate 5 — No-entry window      (configurable, default 14:30 IST — no new entries)
         — Hard exit time       (15:00 IST — no new signals after this)
  Gate 6 — Duplicate signal     (already have a live position in same direction)
  Gate 7 — Regime check         (regime detector — block counter-trend signals)

Kill switch integration: accepts an optional kill_switch object (D1 KillSwitch).
If not provided, falls back to shared_state["kill_switch_level"] as a read cache.
"""
from __future__ import annotations

import structlog
from datetime import datetime, time
from decimal import Decimal
from typing import Optional, Protocol, runtime_checkable

import pytz

from core.regime_detector.regime_detector import MarketRegime
from core.strategy_engine.signal_generator import Signal


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

    All 8 gates execute in strict order. First failure = signal dropped.
    Gate 0 asserts paper mode — it crashes hard on misconfiguration by design.
    """

    def __init__(
        self,
        kill_switch: Optional[KillSwitchProtocol] = None,
        regime_detector=None,
    ) -> None:
        """
        Args:
            kill_switch: Optional KillSwitch instance (D1).
                         If None, falls back to shared_state["kill_switch_level"].
            regime_detector: Optional RegimeDetector instance.
                             If None, Gate 7 is skipped (backward compatible).
        """
        self._kill_switch = kill_switch
        self._regime_detector = regime_detector

    def check(
        self,
        signal: Signal,
        shared_state: dict,
        config: dict,
    ) -> tuple[bool, str]:
        """
        Run the signal through all 8 gates in order.

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

        # Gate 5: time-based entry restrictions
        now_ist = datetime.now(IST)

        # 5a: hard exit — 15:00 IST (belt-and-suspenders; B2 flag also blocks)
        if now_ist.time() >= HARD_EXIT_TIME:
            log.debug(
                "risk_gate_blocked", gate=5, reason="HARD_EXIT_TIME_REACHED",
                symbol=signal.symbol,
            )
            return False, "HARD_EXIT_TIME_REACHED"

        # 5b: no-entry window — default 14:30 IST (configurable)
        no_entry_str = config.get("trading_hours", {}).get("no_entry_after", "14:30")
        h, m = map(int, no_entry_str.split(":"))
        no_entry_time = time(h, m)
        if now_ist.time() >= no_entry_time:
            log.debug(
                "risk_gate_blocked", gate=5, reason="NO_ENTRY_WINDOW",
                symbol=signal.symbol, direction=signal.direction,
                current_time=now_ist.time().isoformat(),
                cutoff=no_entry_str,
            )
            return False, "NO_ENTRY_WINDOW"

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

        # Gate 7: regime — block counter-trend signals
        if self._regime_detector is not None:
            if signal.direction == "LONG" and not self._regime_detector.is_long_allowed():
                regime = self._regime_detector.current_regime().value
                reason = f"REGIME_BLOCKED_{regime.upper()}"
                log.debug(
                    "risk_gate_blocked", gate=7, reason=reason,
                    symbol=signal.symbol, direction="LONG", regime=regime,
                )
                return False, reason

            if signal.direction == "SHORT" and not self._regime_detector.is_short_allowed():
                regime = self._regime_detector.current_regime().value
                reason = f"REGIME_BLOCKED_{regime.upper()}"
                log.debug(
                    "risk_gate_blocked", gate=7, reason=reason,
                    symbol=signal.symbol, direction="SHORT", regime=regime,
                )
                return False, reason

            # CRASH + SHORT: extra volume confirmation (volume_ratio > 2.0)
            if (self._regime_detector.current_regime() == MarketRegime.CRASH
                    and signal.direction == "SHORT"
                    and signal.volume_ratio <= Decimal("2.0")):
                reason = "REGIME_CRASH_LOW_VOLUME_SHORT"
                log.debug(
                    "risk_gate_blocked", gate=7, reason=reason,
                    symbol=signal.symbol,
                    volume_ratio=float(signal.volume_ratio),
                )
                return False, reason

        return True, "OK"
