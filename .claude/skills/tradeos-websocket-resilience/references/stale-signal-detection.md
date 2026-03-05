# Stale Signal Detection — TradeOS D3

## The Rule

A signal generated before a disconnect is **dangerous** after reconnect — the market moved. The 5-minute threshold is non-negotiable.

```python
signal_age = datetime.now(IST) - signal.generated_at

if signal_age > timedelta(minutes=5):
    # DEAD SIGNAL — discard
else:
    # LIVE SIGNAL — pass to strategy
```

## Why 5 Minutes?

In a 1-minute OHLCV strategy (S1 Intraday Momentum), a signal from 5+ minutes ago:
- Has missed at least 5 candles of price movement
- The entry price assumption is stale
- Risk/reward calculation is invalid
- Acting on it is equivalent to placing a blind order

## Implementation

```python
import structlog
from datetime import datetime, timedelta
import pytz
from dataclasses import dataclass

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")

SIGNAL_MAX_AGE_MINUTES = 5


@dataclass
class Signal:
    symbol: str
    direction: str          # "BUY" or "SELL"
    generated_at: datetime  # Must be IST-aware
    entry_price: float
    strategy: str


def is_signal_stale(signal: Signal) -> bool:
    """Returns True if signal is older than SIGNAL_MAX_AGE_MINUTES."""
    now_ist = datetime.now(IST)
    # Ensure signal.generated_at is timezone-aware
    if signal.generated_at.tzinfo is None:
        signal_time = IST.localize(signal.generated_at)
    else:
        signal_time = signal.generated_at
    age = now_ist - signal_time
    return age > timedelta(minutes=SIGNAL_MAX_AGE_MINUTES)


def check_signal_freshness(signal: Signal) -> bool:
    """
    Gate check — call before passing signal to strategy engine after reconnect.
    Returns True if signal is fresh and should be acted on.
    Returns False if signal is stale and must be discarded.
    """
    now_ist = datetime.now(IST)
    if signal.generated_at.tzinfo is None:
        signal_time = IST.localize(signal.generated_at)
    else:
        signal_time = signal.generated_at

    age = now_ist - signal_time
    age_minutes = age.total_seconds() / 60

    if age > timedelta(minutes=SIGNAL_MAX_AGE_MINUTES):
        log.warning(
            "stale_signal_discarded",
            symbol=signal.symbol,
            strategy=signal.strategy,
            direction=signal.direction,
            generated_at=signal.generated_at.isoformat(),
            age_minutes=round(age_minutes, 2),
            threshold_minutes=SIGNAL_MAX_AGE_MINUTES,
        )
        return False  # Discard

    log.debug(
        "signal_fresh",
        symbol=signal.symbol,
        age_minutes=round(age_minutes, 2),
    )
    return True  # Pass through
```

## Post-Reconnect Signal Processing

```python
async def process_pending_signals_after_reconnect(
    pending_signals: list[Signal],
    kite,
    strategy_queue: asyncio.Queue,
) -> None:
    """
    Called immediately after successful reconnect.
    Re-evaluates all pending signals with current prices.
    """
    if not pending_signals:
        return

    log.info(
        "post_reconnect_signal_check",
        pending_count=len(pending_signals),
    )

    for signal in pending_signals:
        # Step 1: Age check
        if not check_signal_freshness(signal):
            continue  # Discarded — already logged

        # Step 2: Re-fetch current price (market moved during disconnect)
        try:
            current_ltp = await asyncio.to_thread(
                kite.ltp,
                [f"NSE:{signal.symbol}"]
            )
            current_price = current_ltp[f"NSE:{signal.symbol}"]["last_price"]
        except Exception as e:
            log.error(
                "post_reconnect_price_fetch_failed",
                symbol=signal.symbol,
                error=str(e),
            )
            continue  # Cannot evaluate without current price

        # Step 3: Price drift check — if price moved > 1% from signal entry, discard
        price_drift_pct = abs(current_price - signal.entry_price) / signal.entry_price
        if price_drift_pct > 0.01:
            log.warning(
                "signal_price_drift_discarded",
                symbol=signal.symbol,
                signal_price=signal.entry_price,
                current_price=current_price,
                drift_pct=round(price_drift_pct * 100, 2),
            )
            continue

        # Step 4: Pass to strategy queue with updated price
        signal.entry_price = current_price
        await strategy_queue.put(signal)
        log.info(
            "post_reconnect_signal_queued",
            symbol=signal.symbol,
            direction=signal.direction,
            current_price=current_price,
        )
```

## IST Timezone Handling

Always generate signals with IST-aware timestamps:

```python
from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")

# CORRECT — always IST-aware
generated_at = datetime.now(IST)

# WRONG — naive datetime, breaks age calculation across DST
generated_at = datetime.now()

# WRONG — UTC then localize works but is error-prone
generated_at = datetime.utcnow().replace(tzinfo=pytz.utc).astimezone(IST)
```

## Testing Stale Signal Detection

```python
import pytest
from datetime import datetime, timedelta
import pytz

IST = pytz.timezone("Asia/Kolkata")


def test_fresh_signal_passes():
    signal = Signal(
        symbol="RELIANCE",
        direction="BUY",
        generated_at=datetime.now(IST) - timedelta(minutes=2),
        entry_price=2500.0,
        strategy="s1",
    )
    assert check_signal_freshness(signal) is True


def test_stale_signal_discarded():
    signal = Signal(
        symbol="RELIANCE",
        direction="BUY",
        generated_at=datetime.now(IST) - timedelta(minutes=6),
        entry_price=2500.0,
        strategy="s1",
    )
    assert check_signal_freshness(signal) is False


def test_exactly_5_minutes_is_stale():
    """Boundary: exactly 5 minutes is stale (> not >=)."""
    signal = Signal(
        symbol="INFY",
        direction="SELL",
        generated_at=datetime.now(IST) - timedelta(minutes=5, seconds=1),
        entry_price=1800.0,
        strategy="s1",
    )
    assert check_signal_freshness(signal) is False
```
