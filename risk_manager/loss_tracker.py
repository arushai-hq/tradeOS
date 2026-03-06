"""
TradeOS — Loss Tracker (Risk Manager)

Tracks consecutive losses and resets on wins.
Feeds shared_state["consecutive_losses"] which D1 kill switch uses
for the compound L1-T1 trigger condition:

    consecutive_losses >= 5 AND daily_pnl_pct <= -0.015

This class ONLY tracks the count. D1 KillSwitch owns the trigger decision.

Reset events:
  - On win (net_pnl >= 0): reset to 0
  - on_session_start(): reset to 0 at start of each trading day
  - on_kill_switch_reset(): reset to 0 on manual kill switch reset
    (critical gap fix: prevents immediate re-trigger after manual reset)
"""
from __future__ import annotations

import structlog
from decimal import Decimal

log = structlog.get_logger()


class LossTracker:
    """
    Consecutive loss counter with shared_state feed for D1 kill switch.

    Breakeven trades (net_pnl == 0) are treated as wins → counter resets.
    """

    def __init__(self, shared_state: dict) -> None:
        """
        Args:
            shared_state: D6 shared state dict.
                         Writes shared_state["consecutive_losses"] on every update.
        """
        self._shared_state: dict = shared_state
        self._consecutive_losses: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_trade_close(self, net_pnl: Decimal) -> None:
        """
        Update consecutive loss counter after a trade closes.

        Args:
            net_pnl: Net P&L of the closed trade (after charges).
                     Negative → loss; zero or positive → win (counter resets).
        """
        if net_pnl < Decimal("0"):
            self._consecutive_losses += 1
            self._shared_state["consecutive_losses"] = self._consecutive_losses
            log.debug(
                "loss_tracker_loss",
                loss_number=self._consecutive_losses,
                consecutive_losses=self._consecutive_losses,
            )
        else:
            # Win or breakeven — reset counter
            self._consecutive_losses = 0
            self._shared_state["consecutive_losses"] = 0
            log.debug("loss_tracker_win", consecutive_losses=0)

    def on_session_start(self) -> None:
        """
        Reset counter at the start of each trading session.

        Called by RiskManager.__aenter__() before market open.
        Counter never carries over across trading days.
        """
        self._consecutive_losses = 0
        self._shared_state["consecutive_losses"] = 0
        log.info("loss_tracker_session_reset", consecutive=0)

    def on_kill_switch_reset(self) -> None:
        """
        Reset counter after manual kill switch reset.

        Critical gap fix (identified in D1 audit): without this reset,
        the kill switch could re-trigger immediately after manual reset
        if consecutive_losses is still >= 5 in shared_state.
        """
        self._consecutive_losses = 0
        self._shared_state["consecutive_losses"] = 0
        log.info("loss_tracker_kill_switch_reset", consecutive=0)

    def get_count(self) -> int:
        """Returns the current consecutive loss count."""
        return self._consecutive_losses
