# Startup & Shutdown Sequences — TradeOS D6

## Startup Sequence (Mandatory Order)

Tasks must start in this exact order. Never deviate.

```python
async def main(config: dict, secrets: dict) -> None:
    import signal as signal_module

    # ── Step 1: Config + state ─────────────────────────────────────────────
    shared_state = _init_shared_state()
    log.info("system_starting", mode=config["system"]["mode"])

    # ── Step 2: Startup reconciliation — BLOCKING ──────────────────────────
    # Must complete with zero mismatches before any task starts.
    recon_result = await reconcile_on_startup(shared_state)
    if recon_result.mismatches:
        log.critical("startup_reconciliation_failed",
                     mismatches=len(recon_result.mismatches))
        raise SystemExit("Cannot start: position mismatch detected")

    # ── Step 3: Start risk_watchdog FIRST ─────────────────────────────────
    # Must be watching before ANY trading activity.
    tasks = [
        asyncio.create_task(
            _resilient_risk_watchdog("risk_watchdog", risk_watchdog_fn, shared_state),
            name="risk_watchdog",
        ),
    ]
    await asyncio.sleep(0)  # yield to let risk_watchdog initialise

    # ── Step 4: Start order_monitor ────────────────────────────────────────
    # Must be tracking orders before WebSocket connects.
    tasks.append(asyncio.create_task(
        resilient_task("order_monitor", order_monitor_fn, shared_state),
        name="order_monitor",
    ))

    # ── Step 5: Start ws_listener ─────────────────────────────────────────
    tasks.append(asyncio.create_task(
        resilient_task("ws_listener", ws_listener_fn, shared_state),
        name="ws_listener",
    ))

    # ── Step 6: Wait for WebSocket connected before starting signal_processor
    await _wait_for_ws_connected(shared_state, timeout_seconds=30)

    # ── Step 7: Start signal_processor ────────────────────────────────────
    # NEVER start before WS is CONNECTED.
    tasks.append(asyncio.create_task(
        resilient_task("signal_processor", signal_processor_fn, shared_state),
        name="signal_processor",
    ))

    # ── Step 8: Start heartbeat LAST ──────────────────────────────────────
    tasks.append(asyncio.create_task(
        resilient_task("heartbeat", heartbeat_fn, shared_state),
        name="heartbeat",
    ))

    log.info("system_ready",
             tasks=[t.get_name() for t in tasks],
             mode=config["system"]["mode"])

    # Register SIGTERM/SIGINT handler
    loop = asyncio.get_running_loop()
    for sig in (signal_module.SIGTERM, signal_module.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown(tasks, shared_state)))

    await asyncio.gather(*tasks, return_exceptions=True)


async def _wait_for_ws_connected(shared_state: dict, timeout_seconds: int = 30) -> None:
    """Block until ws_listener reports WebSocket connected."""
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    while not shared_state.get("ws_connected"):
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError("WebSocket did not connect within startup timeout")
        await asyncio.sleep(0.5)
    log.info("ws_connected_confirmed")
```

---

## Crash Recovery Wrapper

Every task except risk_watchdog auto-restarts after a 5-second pause:

```python
async def resilient_task(name: str, coro_fn, shared_state: dict) -> None:
    """Auto-restart wrapper for non-critical tasks."""
    while True:
        try:
            await coro_fn(shared_state)
        except asyncio.CancelledError:
            raise  # ← NEVER suppress CancelledError
        except Exception as e:
            log.error("task_crashed", task=name, error=str(e), exc_info=True)
            shared_state["tasks_alive"][name] = False
            await asyncio.sleep(5)  # brief pause before restart
            shared_state["tasks_alive"][name] = True
            log.info("task_restarted", task=name)
```

**risk_watchdog variant — no auto-restart:**

```python
async def _resilient_risk_watchdog(name: str, coro_fn, shared_state: dict) -> None:
    """risk_watchdog: crash → Level 3 kill switch immediately."""
    try:
        await coro_fn(shared_state)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.critical("risk_watchdog_crashed", error=str(e), exc_info=True)
        kill_switch.trigger(level=3, reason="risk_watchdog_crashed")
        shared_state["tasks_alive"]["risk_watchdog"] = False
        # Do NOT restart — Level 3 means system is in unknown state
        raise
```

---

## Graceful Shutdown Sequence (SIGTERM / SIGINT)

```python
async def shutdown(tasks: list[asyncio.Task], shared_state: dict) -> None:
    """Graceful shutdown on SIGTERM or SIGINT."""
    log.info("shutdown_initiated")

    # Step 1: Stop new signals (Level 1 kill switch)
    kill_switch.trigger(level=1, reason="graceful_shutdown")

    # Step 2: Wait up to 30s for open orders to settle
    try:
        await asyncio.wait_for(
            _wait_for_orders_to_settle(shared_state),
            timeout=30.0,
        )
        log.info("orders_settled")
    except asyncio.TimeoutError:
        log.warning("shutdown_orders_timeout",
                    open_orders=len(shared_state["open_orders"]))

    # Step 3: Final reconciliation
    try:
        await asyncio.wait_for(
            reconcile_on_shutdown(shared_state),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        log.warning("shutdown_reconciliation_timeout")

    # Step 4: Cancel tasks in reverse startup order
    for task in reversed(tasks):
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # Step 5: Final log + Telegram daily summary
    log.info("system_shutdown_complete",
             fills_today=shared_state["fills_today"],
             daily_pnl_rs=shared_state["daily_pnl_rs"],
             daily_pnl_pct=shared_state["daily_pnl_pct"])

    await send_daily_summary_telegram(shared_state)


async def _wait_for_orders_to_settle(shared_state: dict) -> None:
    """Poll until no open orders remain."""
    while shared_state.get("open_orders"):
        await asyncio.sleep(1)
```

---

## Key Rules Summary

| Rule | Detail |
|------|--------|
| Reconciliation first | `reconcile_on_startup()` must complete before any `create_task()` |
| risk_watchdog first task | Always created before other tasks |
| signal_processor last trading task | Only after `ws_connected=True` confirmed |
| heartbeat absolutely last | Confirms everything running |
| CancelledError never suppressed | `except asyncio.CancelledError: raise` always |
| Shutdown timeout | 30s max for orders, 15s max for recon — never block forever |
| return_exceptions=True | In `asyncio.gather()` so one crash doesn't kill all |
