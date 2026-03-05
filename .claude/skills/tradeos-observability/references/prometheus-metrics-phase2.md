# Prometheus Metrics — TradeOS D4 Phase 2

## Phase Guard Pattern

Metrics are defined at module level but only registered with Prometheus when
`OBSERVABILITY_PHASE=2`. This means Phase 1 can run without `prometheus_client`
installed, and the same code runs in both phases without modification.

```python
import os
import structlog

log = structlog.get_logger()

# Phase detection — default to Phase 1
OBSERVABILITY_PHASE = int(os.environ.get("OBSERVABILITY_PHASE", "1"))


def _register_prometheus_metrics() -> dict:
    """
    Import and register Prometheus metrics.
    Only called when OBSERVABILITY_PHASE == 2.
    Returns a dict of metric objects for use by the rest of the app.
    """
    try:
        from prometheus_client import Counter, Gauge, Histogram, start_http_server

        metrics = {
            "trades_total": Counter(
                "tradeos_trades_total",
                "Total trades placed",
                ["strategy", "direction", "outcome"],  # e.g. ["s1", "long", "win"]
            ),
            "pnl_rupees": Gauge(
                "tradeos_pnl_rupees",
                "Current session PnL in INR",
                ["strategy"],
            ),
            "drawdown_pct": Gauge(
                "tradeos_drawdown_pct",
                "Current drawdown percentage (positive = drawdown)",
            ),
            "api_latency": Histogram(
                "tradeos_api_latency_seconds",
                "Zerodha API call latency in seconds",
                ["endpoint"],  # e.g. "place_order", "orders", "positions"
                buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
            ),
            "ws_reconnects": Counter(
                "tradeos_ws_reconnects_total",
                "WebSocket reconnect count",
            ),
            "kill_switch_level": Gauge(
                "tradeos_kill_switch_level",
                "Current kill switch level (0=inactive, 1=trade_stop, 2=position_stop, 3=system_stop)",
            ),
        }

        # Start HTTP server for Prometheus scraping on port 8000
        start_http_server(8000)
        log.info("prometheus_metrics_registered", port=8000)
        return metrics

    except ImportError:
        log.error("prometheus_client_not_installed",
                  message="Install prometheus_client for Phase 2 metrics")
        return {}


# Module-level metrics dict — empty in Phase 1, populated in Phase 2
_metrics: dict = {}


def init_observability() -> None:
    """
    Call once at startup. Activates Prometheus in Phase 2.
    Safe to call in Phase 1 — no-op for Prometheus.
    """
    global _metrics
    if OBSERVABILITY_PHASE >= 2:
        _metrics = _register_prometheus_metrics()
    else:
        log.info("observability_phase1_active",
                 message="Phase 2 metrics disabled — set OBSERVABILITY_PHASE=2 to enable")
```

---

## Metric Update Pattern

Always check if metric exists before updating — this keeps Phase 1 safe:

```python
def record_trade(strategy: str, direction: str, outcome: str) -> None:
    """outcome: 'win' | 'loss' | 'breakeven'"""
    if counter := _metrics.get("trades_total"):
        counter.labels(strategy=strategy, direction=direction, outcome=outcome).inc()


def update_pnl(strategy: str, pnl_rs: float) -> None:
    if gauge := _metrics.get("pnl_rupees"):
        gauge.labels(strategy=strategy).set(pnl_rs)


def update_drawdown(drawdown_pct: float) -> None:
    """drawdown_pct: positive number representing drawdown magnitude (e.g. 2.5 = -2.5%)"""
    if gauge := _metrics.get("drawdown_pct"):
        gauge.set(drawdown_pct)


def record_api_latency(endpoint: str, duration_seconds: float) -> None:
    if hist := _metrics.get("api_latency"):
        hist.labels(endpoint=endpoint).observe(duration_seconds)


def record_ws_reconnect() -> None:
    if counter := _metrics.get("ws_reconnects"):
        counter.inc()


def update_kill_switch_level(level: int) -> None:
    """level: 0=inactive, 1=trade_stop, 2=position_stop, 3=system_stop"""
    if gauge := _metrics.get("kill_switch_level"):
        gauge.set(level)
```

---

## API Latency Context Manager

```python
import time
from contextlib import asynccontextmanager

@asynccontextmanager
async def track_api_call(endpoint: str):
    """
    Usage:
        async with track_api_call("place_order"):
            result = await asyncio.to_thread(kite.place_order, ...)
    """
    start = time.monotonic()
    try:
        yield
    finally:
        elapsed = time.monotonic() - start
        record_api_latency(endpoint, elapsed)
        # Also log for Phase 1 visibility
        if elapsed > 1.0:
            log.warning("api_call_slow", endpoint=endpoint,
                        duration_seconds=round(elapsed, 3))
```

---

## Metric Definitions Reference

| Metric | Type | Labels | What to track |
|--------|------|--------|---------------|
| `tradeos_trades_total` | Counter | strategy, direction, outcome | Increment on each fill |
| `tradeos_pnl_rupees` | Gauge | strategy | Update after each fill/close |
| `tradeos_drawdown_pct` | Gauge | none | Update in risk watchdog every 1s |
| `tradeos_api_latency_seconds` | Histogram | endpoint | Wrap every `kite.*` call |
| `tradeos_ws_reconnects_total` | Counter | none | Increment in on_connect after disconnect |
| `tradeos_kill_switch_level` | Gauge | none | Update whenever kill switch fires/resets |

---

## Prometheus + Grafana VPS Setup (Phase 2 activation)

When ready to activate Phase 2 on the VPS:

1. Install: `pip install prometheus_client`
2. Set env: `OBSERVABILITY_PHASE=2` in systemd unit
3. Add to `prometheus.yml`:
   ```yaml
   scrape_configs:
     - job_name: tradeos
       static_configs:
         - targets: ['localhost:8000']
       scrape_interval: 10s
   ```
4. Import TradeOS Grafana dashboard (JSON in `docs/grafana/`)
5. Configure Alertmanager → Telegram for Prometheus alerts

TradeOS will start exporting metrics on port 8000 automatically.
No code changes required — only env var change.
