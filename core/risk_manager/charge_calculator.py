"""
TradeOS — Charge Calculator (Risk Manager)

Calculates Zerodha intraday MIS charges for NSE equity trades.

# Zerodha MIS NSE equity rates — verified 2024
Brokerage:     min(₹20, 0.03% of turnover) per leg
STT:           0.025% of sell-side turnover only
Exchange txn:  0.00345% of total turnover (both legs)
SEBI charges:  ₹10 per crore of total turnover
Stamp duty:    0.003% of buy-side turnover only
GST:           18% on (brokerage + exchange txn + SEBI charges)

Direction rules:
  LONG:  entry = buy leg, exit = sell leg
         STT on exit, stamp duty on entry
  SHORT: entry = sell leg, exit = buy leg
         STT on entry, stamp duty on exit
"""
from __future__ import annotations

import structlog
from dataclasses import dataclass
from decimal import Decimal

log = structlog.get_logger()

# Zerodha MIS NSE equity rates — verified 2024
_BROKERAGE_RATE: Decimal = Decimal("0.0003")      # 0.03% per leg
_BROKERAGE_CAP: Decimal = Decimal("20")            # ₹20 per leg
_STT_RATE: Decimal = Decimal("0.00025")            # 0.025% sell-side
_EXCHANGE_TXN_RATE: Decimal = Decimal("0.0000345") # 0.00345% both legs
_SEBI_RATE: Decimal = Decimal("0.000001")          # ₹10 per crore = 10/10000000
_STAMP_DUTY_RATE: Decimal = Decimal("0.00003")     # 0.003% buy-side
_GST_RATE: Decimal = Decimal("0.18")               # 18%


@dataclass
class ChargeBreakdown:
    """Full charge breakdown for a single intraday trade (both legs)."""

    brokerage: Decimal      # both legs combined
    stt: Decimal            # sell-side turnover only
    exchange_txn: Decimal   # both legs combined
    sebi: Decimal           # both legs combined
    stamp_duty: Decimal     # buy-side only
    gst: Decimal            # 18% on brokerage + exchange_txn + sebi
    total: Decimal          # sum of all above — always equals sum exactly


class ChargeCalculator:
    """
    Computes Zerodha MIS intraday charge breakdown (NSE equity, 2024 rates).

    All arithmetic uses Decimal. No float at any stage.
    """

    def calculate(
        self,
        qty: int,
        entry_price: Decimal,
        exit_price: Decimal,
        direction: str,
    ) -> ChargeBreakdown:
        """
        Calculate the full charge breakdown for a closed trade.

        Args:
            qty:         Number of shares traded.
            entry_price: Entry fill price (Decimal).
            exit_price:  Exit fill price (Decimal).
            direction:   'LONG' or 'SHORT'.

        Returns:
            ChargeBreakdown with all components and total.
        """
        qty_d: Decimal = Decimal(str(qty))

        turnover_entry: Decimal = qty_d * entry_price
        turnover_exit: Decimal = qty_d * exit_price
        total_turnover: Decimal = turnover_entry + turnover_exit

        # Brokerage: min(₹20, 0.03% of leg turnover), per leg
        brokerage_entry: Decimal = min(_BROKERAGE_CAP, turnover_entry * _BROKERAGE_RATE)
        brokerage_exit: Decimal = min(_BROKERAGE_CAP, turnover_exit * _BROKERAGE_RATE)
        brokerage: Decimal = brokerage_entry + brokerage_exit

        # Side identification for STT and stamp duty
        if direction == "LONG":
            sell_turnover = turnover_exit    # exit is the sell leg for LONG
            buy_turnover = turnover_entry    # entry is the buy leg for LONG
        else:  # SHORT
            sell_turnover = turnover_entry   # entry is the sell leg for SHORT
            buy_turnover = turnover_exit     # exit is the buy (cover) leg for SHORT

        # STT: 0.025% on sell-side turnover only
        stt: Decimal = sell_turnover * _STT_RATE

        # Exchange transaction: 0.00345% of total turnover (both legs)
        exchange_txn: Decimal = total_turnover * _EXCHANGE_TXN_RATE

        # SEBI charges: ₹10 per crore = 0.000001 of total turnover
        sebi: Decimal = total_turnover * _SEBI_RATE

        # Stamp duty: 0.003% of buy-side turnover only
        stamp_duty: Decimal = buy_turnover * _STAMP_DUTY_RATE

        # GST: 18% on (brokerage + exchange_txn + sebi)
        gst: Decimal = (brokerage + exchange_txn + sebi) * _GST_RATE

        total: Decimal = brokerage + stt + exchange_txn + sebi + stamp_duty + gst

        log.debug(
            "charge_calculator",
            qty=qty,
            direction=direction,
            entry=float(entry_price),
            exit=float(exit_price),
            total_charges=float(total),
        )

        return ChargeBreakdown(
            brokerage=brokerage,
            stt=stt,
            exchange_txn=exchange_txn,
            sebi=sebi,
            stamp_duty=stamp_duty,
            gst=gst,
            total=total,
        )
