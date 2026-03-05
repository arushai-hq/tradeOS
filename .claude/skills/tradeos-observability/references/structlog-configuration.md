# structlog Configuration — TradeOS D4

## Processor Chain (Phase 1 — exact order required)

```python
import logging
import logging.handlers
import sys
from pathlib import Path
import structlog

LOG_PATH = Path("logs/tradeos.log")
LOG_PATH.parent.mkdir(exist_ok=True)


def configure_logging() -> None:
    """
    Configure structlog for TradeOS Phase 1.
    Call once at startup — before any log.info() call.

    Produces single-line JSON on every event:
      {"timestamp": "2026-03-05T11:23:45.123+05:30", "level": "info",
       "logger": "data_engine.ws_listener", "event": "ws_connected", ...}
    """
    # --- stdlib logging (receives structlog output) ---
    stdlib_handler_file = logging.handlers.RotatingFileHandler(
        LOG_PATH,
        maxBytes=10 * 1024 * 1024,   # 10 MB per file
        backupCount=7,                # 7 rotations = 1 week retention
        encoding="utf-8",
        mode="a",                     # append — never truncate
    )
    stdlib_handler_file.setLevel(logging.DEBUG)

    stdlib_handler_stderr = logging.StreamHandler(sys.stderr)
    stdlib_handler_stderr.setLevel(logging.WARNING)  # WARNING+ to terminal

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(stdlib_handler_file)
    root_logger.addHandler(stdlib_handler_stderr)

    # --- structlog processor chain ---
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,               # 1: adds "level"
            structlog.stdlib.add_logger_name,              # 2: adds "logger"
            structlog.processors.TimeStamper(             # 3: IST timestamp
                fmt="iso",
                utc=False,                                #    local time = IST (set TZ=Asia/Kolkata)
            ),
            structlog.processors.StackInfoRenderer,       # 4: exception traces
            structlog.processors.JSONRenderer(),           # 5: single-line JSON output
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
```

## Environment Requirement

Set `TZ=Asia/Kolkata` before starting the process so `TimeStamper(utc=False)`
produces IST timestamps:

```bash
# In your systemd unit or launch script:
Environment=TZ=Asia/Kolkata
```

Or in code (must be before any datetime call):
```python
import os
os.environ["TZ"] = "Asia/Kolkata"
import time; time.tzset()   # only on Linux/macOS
```

## Module-level Logger Pattern

Every module gets its own logger — the processor chain adds the module name
automatically via `add_logger_name`:

```python
import structlog
log = structlog.get_logger()   # logger name = calling module's __name__
```

Do NOT pass a name string: `structlog.get_logger("my_name")` is rarely needed.

## Log Rotation

The `RotatingFileHandler` handles rotation automatically:
- File rolls when it hits 10 MB
- Keeps 7 backup files: `tradeos.log.1` through `tradeos.log.7`
- `tradeos.log` is always the current file
- Do NOT `rm logs/tradeos.log` manually — this breaks the file handle

```
logs/
├── tradeos.log        ← current (appending)
├── tradeos.log.1      ← last rotation
├── tradeos.log.2
...
└── tradeos.log.7      ← oldest (auto-deleted on next rotation)
```

## Mandatory Base Fields

Every log entry automatically contains these from the processor chain:
```json
{
  "timestamp": "2026-03-05T11:23:45.123456+05:30",
  "level": "info",
  "logger": "execution_engine.order_placer",
  "event": "order_placed"
}
```

Additional fields are the domain-specific payload. See `event-schemas.md` for
required fields per event type.

## Async Safety

structlog's `JSONRenderer` runs synchronously but is CPU-bound and fast — it
does not block on I/O. The `RotatingFileHandler` does perform synchronous file
writes, which is acceptable for low-frequency events (orders, signals, errors).

For the tick processing hot path (on_ticks callback), do NOT log every tick
to file — log only anomalies. If you must log from the hot path:

```python
# Fine for occasional events in coroutines:
log.info("signal_generated", symbol=symbol, ...)

# For extremely high-frequency: use asyncio.to_thread for batch writes
await asyncio.to_thread(flush_pending_logs)
```

## Phase 2 Addition (Loki)

When activating Phase 2, add a `LokiHandler` to the stdlib root logger —
the structlog chain stays unchanged. No existing log calls need modification.
