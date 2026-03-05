# Mismatch Resolution Modes

When the comparison algorithm finds a mismatch, there are 3 resolution modes.
**The default mode is MANUAL** — auto-correction is never enabled unless explicitly configured.

## Mode 1: Manual (Default)

**What it does**: Locks the mismatched instrument, logs and alerts, waits for human action.
**When to use**: Always, unless AUTO_ADJUST is explicitly set in config.

```python
def _handle_mismatch_manual(shared_state: dict, mismatch: dict) -> None:
    token = mismatch["instrument_token"]
    symbol = mismatch["tradingsymbol"]
    mtype = mismatch["mismatch_type"]

    # Lock the instrument (see instrument-lock-mechanism.md)
    shared_state["locked_instruments"].add(token)

    if mismatch["severity"] == "critical":
        log.critical(
            "position_mismatch",
            instrument=symbol,
            mismatch_type=mtype,
            broker_qty=mismatch["broker_qty"],
            local_qty=mismatch["local_qty"],
            resolution="manual_lock",
        )
        send_critical_alert(
            f"POSITION MISMATCH — {symbol}\n"
            f"Type: {mtype}\n"
            f"Broker qty: {mismatch['broker_qty']}\n"
            f"Local qty: {mismatch['local_qty']}\n"
            f"Instrument LOCKED — manual intervention required"
        )
    else:
        log.warning(
            "position_mismatch",
            instrument=symbol,
            mismatch_type=mtype,
            broker_qty=mismatch["broker_qty"],
            local_qty=mismatch["local_qty"],
            resolution="manual_lock",
        )
        send_warning_alert(
            f"Position mismatch: {symbol} — locked pending review"
        )
```

**Human resolution**: Operator must manually reconcile (e.g. via Zerodha console), then unlock:
```python
shared_state["locked_instruments"].discard(token)
```

---

## Mode 2: Auto-Adjust (Disabled by Default)

**What it does**: Updates local state to match broker. No orders placed, just state sync.
**When to use**: Only when `config["reconciliation"]["auto_adjust"] == True`.

**Safe to auto-adjust**: `qty_mismatch` and `missing_local` types.
**Never auto-adjust**: `ghost_position` type — ghost positions require manual handling.

```python
def _handle_mismatch_auto_adjust(shared_state: dict, mismatch: dict, kite) -> None:
    token = mismatch["instrument_token"]
    mtype = mismatch["mismatch_type"]

    if mtype == "ghost_position":
        # NEVER auto-adjust ghost positions — fall back to manual
        _handle_mismatch_manual(shared_state, mismatch)
        return

    # Update local state to match broker
    if mismatch["broker_qty"] == 0:
        # Broker says flat — clear local position
        shared_state["position_state"].pop(token, None)
    else:
        # Broker has position — update local quantity
        if token in shared_state["position_state"]:
            shared_state["position_state"][token]["quantity"] = mismatch["broker_qty"]
        # Note: if no local record at all, this is a ghost — handled above

    log.warning(
        "position_auto_adjusted",
        instrument=mismatch["tradingsymbol"],
        old_qty=mismatch["local_qty"],
        new_qty=mismatch["broker_qty"],
        mismatch_type=mtype,
    )
    send_warning_alert(
        f"Auto-adjusted: {mismatch['tradingsymbol']} "
        f"qty {mismatch['local_qty']} → {mismatch['broker_qty']}"
    )
```

**Config guard** — always check before calling auto-adjust:
```python
if config.get("reconciliation", {}).get("auto_adjust", False):
    _handle_mismatch_auto_adjust(shared_state, mismatch, kite)
else:
    _handle_mismatch_manual(shared_state, mismatch)
```

---

## Mode 3: Ghost Close Protocol

**What it does**: Broker has a position but local state has no record of it. This is the most dangerous mismatch.
**Resolution**: Lock instrument, log CRITICAL, send Telegram CRITICAL alert, do NOT auto-close.

Ghost positions arise from:
- Manual trades placed directly on the broker terminal
- Orders placed during a system crash before state was saved
- Partial fills followed by a system restart

```python
def _handle_ghost_position(shared_state: dict, mismatch: dict) -> None:
    """
    Ghost position: broker qty != 0, local qty == 0.
    NEVER auto-close. Operator must decide.
    """
    token = mismatch["instrument_token"]
    symbol = mismatch["tradingsymbol"]
    broker_qty = mismatch["broker_qty"]

    # Lock — no new signals or orders for this instrument
    shared_state["locked_instruments"].add(token)

    # Add a ghost marker to position_state so it shows up in heartbeat
    shared_state["position_state"][token] = {
        "instrument_token": token,
        "tradingsymbol": symbol,
        "quantity": broker_qty,       # from broker
        "average_price": 0.0,         # unknown — we didn't place this
        "is_ghost": True,             # flag for UI / heartbeat
        "detected_at": datetime.now(IST).isoformat(),
    }

    log.critical(
        "ghost_position_detected",
        instrument=symbol,
        broker_qty=broker_qty,
        action="instrument_locked_awaiting_manual_resolution",
    )
    send_critical_alert(
        f"GHOST POSITION DETECTED: {symbol}\n"
        f"Broker qty: {broker_qty}\n"
        f"No local record found.\n"
        f"Instrument LOCKED. Manual close required via Zerodha console."
    )
```

**Why not auto-close?** A ghost position may be from a hedge, a manual safety trade, or an operator action. Auto-closing it could create a naked position or realize an unexpected loss. Always escalate to human.

---

## Mode Selection Logic

```python
def _handle_mismatch(shared_state: dict, mismatch: dict, config: dict) -> None:
    if mismatch["mismatch_type"] == "ghost_position":
        _handle_ghost_position(shared_state, mismatch)
        return

    if config.get("reconciliation", {}).get("auto_adjust", False):
        _handle_mismatch_auto_adjust(shared_state, mismatch, config)
    else:
        _handle_mismatch_manual(shared_state, mismatch)
```
