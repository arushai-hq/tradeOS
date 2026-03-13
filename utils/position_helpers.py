"""
TradeOS — Position Field Resolution Helpers

shared_state["open_positions"] is written by two components:
  - PnlTracker:  {"direction": "LONG/SHORT", "entry_price": Decimal, "qty": int}
  - ExitManager: {"side": "BUY/SELL", "avg_price": float, "qty": ±int}

ExitManager writes AFTER PnlTracker, so shared_state typically contains
the ExitManager schema (avg_price / side / signed qty).  Any code reading
position data from shared_state MUST use resolve_position_fields() to
handle both schemas transparently.

See: B7, B12, B13 — all caused by reading the wrong field names.
"""


def resolve_position_fields(pos: dict) -> tuple[float, str, int]:
    """
    Extract (entry_price, direction, qty) from a position dict,
    handling both PnlTracker and ExitManager field name schemas.

    Returns:
        (entry_price, direction, qty) — always positive qty.
    """
    entry = float(pos.get("entry_price", pos.get("avg_price", 0.0)))

    direction = pos.get("direction")
    if direction is None:
        side = pos.get("side", "BUY")
        direction = "LONG" if side == "BUY" else "SHORT"

    qty = abs(int(pos.get("qty", 0)))

    return entry, direction, qty
