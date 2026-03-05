# Five-Gate Validation Pipeline — TradeOS D5

## Complete TickValidator Implementation

```python
import structlog
import pytz
from datetime import datetime
from typing import Optional

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")


class TickValidator:
    """
    5-gate tick validation pipeline for Zerodha KiteConnect ticks.
    All gates are O(1). Never raises. Never calls external services.
    """

    def __init__(self):
        # instrument_token → previous day close price
        self.prev_close_cache: dict[int, float] = {}
        # instrument_token → {price: float, ts: datetime}
        self.last_tick: dict[int, dict] = {}
        # instrument_token → {gate_number: count}
        self.bad_tick_count: dict[int, dict[int, int]] = {}

    def validate(self, tick) -> bool:
        """
        Run tick through all 5 gates in order.
        Returns True if tick is valid and should be processed.
        Returns False if tick should be discarded.
        Never raises an exception — all errors are logged and discarded.
        """
        if not self._gate1_zero_price(tick):
            return False
        if not self._gate2_circuit_breaker(tick):
            return False
        if not self._gate3_negative_volume(tick):
            return False
        if not self._gate4_staleness(tick):
            return False
        if not self._gate5_duplicate(tick):
            return False

        # All gates passed — update last-seen cache
        self.last_tick[tick.instrument_token] = {
            "price": tick.last_price,
            "ts": tick.exchange_timestamp,
        }
        return True

    # ------------------------------------------------------------------ #
    # GATE 1 — Zero price filter
    # ------------------------------------------------------------------ #
    def _gate1_zero_price(self, tick) -> bool:
        """
        Zerodha occasionally sends last_price=0.0 during instrument init.
        Any non-positive price is unusable — discard immediately.
        """
        price = getattr(tick, "last_price", None)
        if price is None or price <= 0:
            symbol = getattr(tick, "tradingsymbol", tick.instrument_token)
            log.warning("zero_price_tick",
                        symbol=symbol,
                        price=price,
                        instrument_token=tick.instrument_token)
            self._increment_bad_tick(tick.instrument_token, gate=1)
            return False
        return True

    # ------------------------------------------------------------------ #
    # GATE 2 — NSE circuit breaker filter (±20%)
    # ------------------------------------------------------------------ #
    def _gate2_circuit_breaker(self, tick) -> bool:
        """
        NSE applies ±20% daily circuit breakers on individual stocks.
        A tick outside this range is a Zerodha data error.

        If prev_close is unavailable: PASS (never block on missing data).
        """
        token = tick.instrument_token
        prev_close = self.prev_close_cache.get(token)

        if prev_close is None:
            return True  # No reference data — pass through

        price = tick.last_price
        deviation = abs(price - prev_close) / prev_close
        if deviation > 0.20:
            symbol = getattr(tick, "tradingsymbol", token)
            log.warning("circuit_breaker_tick",
                        symbol=symbol,
                        price=price,
                        prev_close=prev_close,
                        deviation_pct=round(deviation * 100, 2))
            self._increment_bad_tick(token, gate=2)
            return False

        # Also: update prev_close_cache from ohlc.close if available
        ohlc = getattr(tick, "ohlc", None)
        if ohlc and hasattr(ohlc, "close") and ohlc.close and token not in self.prev_close_cache:
            self.prev_close_cache[token] = ohlc.close

        return True

    # ------------------------------------------------------------------ #
    # GATE 3 — Negative volume filter
    # ------------------------------------------------------------------ #
    def _gate3_negative_volume(self, tick) -> bool:
        """
        volume_traded can be None or negative on bad KiteConnect packets.
        Both are invalid — discard.
        """
        volume = getattr(tick, "volume_traded", None)
        if volume is None or volume < 0:
            symbol = getattr(tick, "tradingsymbol", tick.instrument_token)
            log.warning("negative_volume_tick",
                        symbol=symbol,
                        volume=volume,
                        instrument_token=tick.instrument_token)
            self._increment_bad_tick(tick.instrument_token, gate=3)
            return False
        return True

    # ------------------------------------------------------------------ #
    # GATE 4 — Staleness filter (5-second threshold)
    # ------------------------------------------------------------------ #
    def _gate4_staleness(self, tick) -> bool:
        """
        Ticks older than 5 seconds during live market hours are dangerous.
        Strategy logic must not act on prices that no longer reflect market.

        Uses tick.exchange_timestamp (exchange time), NOT datetime.now().
        exchange_timestamp is provided by Zerodha in the tick payload.
        """
        exchange_ts = getattr(tick, "exchange_timestamp", None)
        if exchange_ts is None:
            # Missing timestamp → treat as valid (can't determine age)
            return True

        now_ist = datetime.now(IST)
        # Make exchange_ts timezone-aware if naive
        if exchange_ts.tzinfo is None:
            exchange_ts = IST.localize(exchange_ts)

        age_seconds = (now_ist - exchange_ts).total_seconds()
        if age_seconds > 5:
            symbol = getattr(tick, "tradingsymbol", tick.instrument_token)
            log.warning("stale_tick",
                        symbol=symbol,
                        age_seconds=round(age_seconds, 2),
                        exchange_timestamp=exchange_ts.isoformat())
            self._increment_bad_tick(tick.instrument_token, gate=4)
            return False
        return True

    # ------------------------------------------------------------------ #
    # GATE 5 — Duplicate filter (SILENT discard)
    # ------------------------------------------------------------------ #
    def _gate5_duplicate(self, tick) -> bool:
        """
        KiteConnect sends duplicate ticks on reconnect — same price AND
        same exchange_timestamp. These are silent discards (no log).

        Why silent? Reconnect duplicates can arrive in bursts of hundreds.
        Logging every one would spam logs and is entirely unhelpful.
        """
        token = tick.instrument_token
        last = self.last_tick.get(token)

        if last is None:
            return True  # First tick for this instrument — always valid

        same_price = (tick.last_price == last["price"])
        same_ts = (tick.exchange_timestamp == last["ts"])

        if same_price and same_ts:
            return False  # Duplicate — silent discard, no log, no counter

        return True

    # ------------------------------------------------------------------ #
    # Internal helper
    # ------------------------------------------------------------------ #
    def _increment_bad_tick(self, token: int, gate: int) -> None:
        """Increment bad tick counter (for bad_tick_monitoring module)."""
        if token not in self.bad_tick_count:
            self.bad_tick_count[token] = {}
        self.bad_tick_count[token][gate] = \
            self.bad_tick_count[token].get(gate, 0) + 1
```

---

## Usage in the Signal Processor

```python
validator = TickValidator()

async def process_ticks(tick_queue: asyncio.Queue) -> None:
    while True:
        ticks = await tick_queue.get()
        for tick in ticks:
            if not validator.validate(tick):
                continue  # discard — validator already logged the reason
            # Tick is clean — forward to strategy
            await strategy_engine.on_tick(tick)
```

## Gate Failure Reference

| Gate | Event name | Fields logged |
|------|-----------|---------------|
| 1 | `zero_price_tick` | symbol, price, instrument_token |
| 2 | `circuit_breaker_tick` | symbol, price, prev_close, deviation_pct |
| 3 | `negative_volume_tick` | symbol, volume, instrument_token |
| 4 | `stale_tick` | symbol, age_seconds, exchange_timestamp |
| 5 | (none — silent) | — |
