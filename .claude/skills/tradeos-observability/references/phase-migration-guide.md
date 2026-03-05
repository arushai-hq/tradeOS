# Phase Migration Guide — TradeOS D4

## Design Principle

Phase 1 and Phase 2 use the same codebase. Activating Phase 2 is a
configuration change, not a rewrite. The golden rule: every Phase 1 component
must work without Phase 2 infrastructure running or installed.

## Phase 1 → Phase 2 Checklist

When scaling from ₹50K to ₹3L+, activate Phase 2 in this order:

### Step 1: Install Phase 2 packages
```bash
pip install prometheus_client loki-handler
```
Phase 1 packages (`structlog`, `httpx`, `pytz`) remain unchanged.

### Step 2: Set environment variable
```bash
# Add to /etc/systemd/system/tradeos.service
Environment=OBSERVABILITY_PHASE=2
Environment=TZ=Asia/Kolkata
```

### Step 3: Add Loki handler to stdlib logging
This is the ONLY code change needed — add one handler to the root logger:

```python
# In configure_logging(), after existing handlers:
if OBSERVABILITY_PHASE >= 2:
    try:
        from loki_handler import LokiHandler
        loki_handler = LokiHandler(
            url="http://localhost:3100/loki/api/v1/push",
            tags={"app": "tradeos", "env": "production"},
            default_formatter=logging.Formatter(),
        )
        loki_handler.setLevel(logging.INFO)
        root_logger.addHandler(loki_handler)
        log.info("loki_handler_registered", url="http://localhost:3100")
    except ImportError:
        log.warning("loki_handler_not_installed")
```

structlog's processor chain is unchanged. All existing log calls automatically
flow to Loki — no modification to event-emitting code.

### Step 4: Start Prometheus (auto on init)
`init_observability()` in `prometheus-metrics-phase2.md` calls
`start_http_server(8000)` automatically when `OBSERVABILITY_PHASE=2`.

### Step 5: Start infrastructure (docker-compose)

```yaml
# docker-compose.phase2.yml — run on same VPS as TradeOS
version: "3.8"
services:
  prometheus:
    image: prom/prometheus:latest
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
    ports: ["9090:9090"]

  loki:
    image: grafana/loki:latest
    ports: ["3100:3100"]

  grafana:
    image: grafana/grafana:latest
    ports: ["3000:3000"]
    environment:
      - GF_AUTH_ANONYMOUS_ENABLED=true
```

```bash
docker-compose -f docker-compose.phase2.yml up -d
```

### Step 6: Verify
```bash
# Check Prometheus scraping:
curl http://localhost:8000/metrics | grep tradeos_

# Check Loki ingestion:
curl "http://localhost:3100/loki/api/v1/query?query={app=\"tradeos\"}" | jq

# Check Grafana dashboard:
open http://localhost:3000
```

---

## Rollback Plan

If Phase 2 infrastructure has issues:
```bash
# Set back to Phase 1 (no restart needed if using env file)
OBSERVABILITY_PHASE=1  # or unset it

# Restart TradeOS
systemctl restart tradeos
```

Phase 1 (structlog + Telegram) resumes immediately. No data loss.

---

## What Stays the Same in Both Phases

| Component | Phase 1 | Phase 2 |
|-----------|---------|---------|
| structlog configuration | ✓ same | ✓ same |
| Event schemas | ✓ same | ✓ same |
| Telegram alerting | ✓ same | ✓ same |
| Log rotation | ✓ same | ✓ same |
| `logs/tradeos.log` file | ✓ same | ✓ same (+ Loki) |
| Prometheus metrics | absent | active |
| Grafana dashboards | absent | active |
| Loki log aggregation | absent | active |

---

## Anti-patterns to Avoid

```python
# ❌ Don't import prometheus_client at module level — crashes in Phase 1
from prometheus_client import Counter
trades_counter = Counter(...)  # dies if prometheus_client not installed

# ✅ Guard with phase check
if OBSERVABILITY_PHASE >= 2:
    from prometheus_client import Counter
    trades_counter = Counter(...)

# ❌ Don't write Phase 2-only code in Phase 1 paths
log.info("tick_received")  # fine
prometheus_client.REGISTRY.get_sample_value(...)  # ❌ Phase 2 dependency

# ✅ Use the _metrics dict pattern from prometheus-metrics-phase2.md
if gauge := _metrics.get("drawdown_pct"):
    gauge.set(current_drawdown)  # no-op in Phase 1, active in Phase 2
```
