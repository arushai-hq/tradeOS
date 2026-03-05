# Bad Tick Monitoring — TradeOS D5

## Purpose

Track bad tick frequency per instrument per gate. If data quality
degrades significantly (> 50 bad ticks per hour for a symbol), alert
via Telegram. Trading continues — this is an observability signal only.

## Data Structure

```python
# bad_tick_count[instrument_token][gate_number] = count_this_hour
bad_tick_count: dict[int, dict[int, int]] = {}
# hour_window_start: datetime of when current hourly window began
hour_window_start: datetime = datetime.now(IST).replace(minute=0, second=0, microsecond=0)
```

## Implementation

```python
import structlog
import asyncio
import pytz
from datetime import datetime

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")

BAD_TICK_ALERT_THRESHOLD = 50   # per instrument per gate per hour


class BadTickMonitor:
    def __init__(self, telegram_alerter=None):
        self.counts: dict[int, dict[int, int]] = {}   # token → {gate: count}
        self.hour_start = self._current_hour()
        self._alerter = telegram_alerter

    def record(self, instrument_token: int, gate: int, symbol: str = "") -> None:
        """
        Record one bad tick for a given instrument and gate.
        Checks alert threshold after every increment.
        """
        self._maybe_reset_hour()

        if instrument_token not in self.counts:
            self.counts[instrument_token] = {}
        gate_counts = self.counts[instrument_token]
        gate_counts[gate] = gate_counts.get(gate, 0) + 1

        count = gate_counts[gate]
        if count == BAD_TICK_ALERT_THRESHOLD:
            # Fire exactly once at threshold (not on every subsequent tick)
            self._on_threshold_reached(instrument_token, gate, count, symbol)

    def _on_threshold_reached(
        self,
        token: int,
        gate: int,
        count: int,
        symbol: str,
    ) -> None:
        log.warning(
            "high_bad_tick_rate",
            symbol=symbol or token,
            gate=gate,
            count=count,
            window="1h",
        )
        if self._alerter is not None:
            # Fire-and-forget — don't await in sync context
            asyncio.create_task(
                self._alerter.send_warning_alert(
                    alert_type=f"bad_tick_{token}_gate{gate}",
                    event="high_bad_tick_rate",
                    fields={
                        "Symbol": symbol or token,
                        "Gate": gate,
                        "Count": f"{count} bad ticks in last hour",
                    },
                )
            )

    def _maybe_reset_hour(self) -> None:
        """Reset all counters when a new hour begins."""
        now = datetime.now(IST)
        if now >= self._next_hour(self.hour_start):
            self.counts.clear()
            self.hour_start = self._current_hour()
            log.debug("bad_tick_counters_reset", hour=self.hour_start.isoformat())

    @staticmethod
    def _current_hour() -> datetime:
        return datetime.now(IST).replace(minute=0, second=0, microsecond=0)

    @staticmethod
    def _next_hour(dt: datetime) -> datetime:
        from datetime import timedelta
        return dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
```

## Integration with TickValidator

```python
class TickValidator:
    def __init__(self, bad_tick_monitor: BadTickMonitor = None):
        self._monitor = bad_tick_monitor or BadTickMonitor()
        ...

    def _increment_bad_tick(self, token: int, gate: int,
                            symbol: str = "") -> None:
        self._monitor.record(token, gate, symbol)
```

## Alert Behaviour

- Threshold: **50 bad ticks per instrument per gate per hour**
- Fires exactly ONCE at threshold — not on every tick above threshold
- Different gates are tracked independently:
  - Gate 1 + Gate 2 alerts for same symbol are independent
  - Gate 1 for RELIANCE and Gate 1 for INFY are independent
- Does NOT stop trading — purely informational
- Telegram message format: `⚠️ Data quality alert: RELIANCE — 50 bad ticks in last hour (Gate 2)`

## Why Not Stop Trading?

A spike in bad ticks usually indicates a temporary data feed issue or
market open turbulence (first few minutes). Stopping trading for data
quality would cause unnecessary missed opportunities. The right response
is to alert and monitor. If the underlying cause is serious (Zerodha feed
degraded), the risk watchdog and kill switch will activate for different
reasons.
