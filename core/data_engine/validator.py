"""
TradeOS — Tick Validator (D5)

5-gate pipeline. Every KiteConnect tick must pass all 5 gates in order
before strategy logic can see it.

Non-negotiable rules:
  - Never raises — all gates return bool; validate() returns dict|None
  - Gate 5 is the only silent failure (no log, no counter)
  - Gates 1–4 log at DEBUG level on each discard
  - prev_close unavailable → Gate 2 PASSES (D5 rule)
  - Bad tick alert: WARNING if any instrument exceeds 50 bad ticks/hour per gate
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import structlog

from core.data_engine.prev_close_cache import PrevCloseCache

log = structlog.get_logger()
IST = ZoneInfo("Asia/Kolkata")

# D5 rule: alert threshold for bad tick monitoring
BAD_TICK_HOURLY_THRESHOLD = 50

# Gate 4 — staleness threshold.
# Zerodha exchange_timestamp = last trade time on exchange,
# not delivery time. Illiquid stocks can have 10-15s gaps
# between trades in slow markets. 30s safely rejects
# pre-market stale ticks (300s+) while accepting all
# legitimate live market ticks. Network latency: ~2ms (not a factor).
STALE_TICK_THRESHOLD_SECONDS = 30


class TickValidator:
    """
    5-gate tick validation pipeline for Zerodha KiteConnect tick dicts.

    Gates execute in strict order. First failure discards the tick immediately.
    All gates are O(1). No external calls. Must complete < 1 ms per tick.
    """

    def __init__(self, prev_close_cache: PrevCloseCache) -> None:
        """
        Args:
            prev_close_cache: Loaded PrevCloseCache. Must have is_loaded()==True
                              before the first validate() call.
        """
        self._prev_close = prev_close_cache
        # Gate 5 state: instrument_token → {"price": float, "ts": datetime}
        self._last_tick: dict[int, dict] = {}
        # Bad-tick counters: instrument_token → {gate_number: count}
        self._bad_tick_count: dict[int, dict[int, int]] = defaultdict(
            lambda: defaultdict(int)
        )

    def validate(self, tick: dict) -> Optional[dict]:
        """
        Run tick through all 5 gates in order.

        Returns the original tick dict when all gates pass.
        Returns None when any gate fails (caller must discard the tick).
        Never raises — any unexpected error returns None.
        """
        try:
            if not self._gate1_nonzero_price(tick):
                return None
            if not self._gate2_circuit_breaker(tick):
                return None
            if not self._gate3_valid_volume(tick):
                return None
            if not self._gate4_freshness(tick):
                return None
            if not self._gate5_duplicate(tick):
                return None

            # All gates passed — update Gate-5 last-seen state
            token = tick.get("instrument_token")
            if token is not None:
                self._last_tick[token] = {
                    "price": tick.get("last_price"),
                    "ts": tick.get("exchange_timestamp"),
                }
            return tick

        except Exception as exc:
            # Safety net: validator must never propagate an exception
            log.error("tick_validator_unexpected_error", error=str(exc), exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Gate 1 — Zero / negative price
    # ------------------------------------------------------------------

    def _gate1_nonzero_price(self, tick: dict) -> bool:
        """Gate 1: Reject ticks where last_price is absent, zero, or negative."""
        price = tick.get("last_price")
        if price is None or price <= 0:
            token = tick.get("instrument_token", "unknown")
            log.debug(
                "tick_rejected",
                gate=1,
                reason="zero_or_nonpositive_price",
                symbol=tick.get("tradingsymbol", token),
                price=price,
            )
            self._inc(token, 1)
            return False
        return True

    # ------------------------------------------------------------------
    # Gate 2 — NSE ±20 % circuit breaker
    # ------------------------------------------------------------------

    def _gate2_circuit_breaker(self, tick: dict) -> bool:
        """
        Gate 2: Reject ticks outside NSE's ±20 % daily circuit-breaker range.

        If prev_close is None (cache miss) → return True (D5 rule: pass).
        """
        token = tick.get("instrument_token")
        prev_close = self._prev_close.get(token) if token is not None else None

        if prev_close is None:
            return True   # D5 rule — no reference data, pass through

        price = tick.get("last_price")
        if price is None:
            return True   # Gate 1 would have caught this; defensive guard

        deviation = abs(price - prev_close) / prev_close
        if deviation > 0.20:
            log.debug(
                "tick_rejected",
                gate=2,
                reason="circuit_breaker",
                symbol=tick.get("tradingsymbol", token),
                price=price,
                prev_close=prev_close,
                deviation_pct=round(deviation * 100, 2),
            )
            self._inc(token, 2)
            return False
        return True

    # ------------------------------------------------------------------
    # Gate 3 — Negative / missing volume
    # ------------------------------------------------------------------

    def _gate3_valid_volume(self, tick: dict) -> bool:
        """Gate 3: Reject ticks where volume_traded is None or < 0."""
        volume = tick.get("volume_traded")
        if volume is None or volume < 0:
            token = tick.get("instrument_token", "unknown")
            log.debug(
                "tick_rejected",
                gate=3,
                reason="invalid_volume",
                symbol=tick.get("tradingsymbol", token),
                volume=volume,
            )
            self._inc(token, 3)
            return False
        return True

    # ------------------------------------------------------------------
    # Gate 4 — Staleness (5-second threshold)
    # ------------------------------------------------------------------

    def _gate4_freshness(self, tick: dict) -> bool:
        """
        Gate 4: Reject ticks older than 5 seconds.

        CRITICAL: Uses tick["exchange_timestamp"] (exchange wall-clock), NOT
        datetime.now(). Strategy must not act on prices that no longer reflect
        the live market.

        All exchange_timestamp values are normalised to IST before comparison.
        Naive timestamps are assumed to be IST wall-clock (KiteConnect default).
        Timezone-aware timestamps (e.g. UTC from some VPS/KiteConnect builds)
        are converted via .astimezone() — preventing false 'stale' rejections
        caused by the VPS system timezone differing from IST.

        Missing exchange_timestamp → pass (cannot determine age).
        """
        exchange_ts = tick.get("exchange_timestamp")
        if exchange_ts is None:
            return True   # Unknown age — pass

        now_ist = datetime.now(IST)

        # Normalise to IST: naive → assume IST wall-clock; aware → convert.
        if exchange_ts.tzinfo is None:
            exchange_ts = exchange_ts.replace(tzinfo=IST)
        else:
            exchange_ts = exchange_ts.astimezone(IST)

        age_seconds = (now_ist - exchange_ts).total_seconds()
        if age_seconds > STALE_TICK_THRESHOLD_SECONDS:
            token = tick.get("instrument_token", "unknown")
            log.debug(
                "tick_rejected",
                gate=4,
                reason="stale_tick",
                symbol=tick.get("tradingsymbol", token),
                age_seconds=round(age_seconds, 2),
                exchange_timestamp=exchange_ts.isoformat(),
            )
            self._inc(token, 4)
            return False
        return True

    # ------------------------------------------------------------------
    # Gate 5 — Duplicate (SILENT discard — no log)
    # ------------------------------------------------------------------

    def _gate5_duplicate(self, tick: dict) -> bool:
        """
        Gate 5: Silently discard duplicate ticks (same price AND same timestamp).

        KiteConnect sends duplicate bursts on reconnect. Logging them would spam
        hundreds of unhelpful lines. No counter increment — duplicates are expected.
        """
        token = tick.get("instrument_token")
        if token is None:
            return True   # No token — cannot deduplicate, pass through

        last = self._last_tick.get(token)

        if last is None:
            return True   # First tick for this instrument — always valid

        same_price = tick.get("last_price") == last.get("price")
        same_ts    = tick.get("exchange_timestamp") == last.get("ts")

        if same_price and same_ts:
            return False  # Silent discard
        return True

    # ------------------------------------------------------------------
    # Bad-tick monitoring
    # ------------------------------------------------------------------

    def _inc(self, token, gate: int) -> None:
        """Increment per-instrument per-gate bad tick counter."""
        if token is None or token == "unknown":
            return
        self._bad_tick_count[token][gate] += 1

    def check_hourly_bad_tick_alert(self) -> None:
        """
        Emit a WARNING for each instrument/gate pair that exceeded the threshold.

        Call once per hour from DataEngine's background monitor task.
        Resets all counters after the check.
        """
        for token, gate_counts in list(self._bad_tick_count.items()):
            for gate, count in gate_counts.items():
                if count >= BAD_TICK_HOURLY_THRESHOLD:
                    log.warning(
                        "bad_tick_rate_alert",
                        instrument_token=token,
                        gate=gate,
                        count_last_hour=count,
                        threshold=BAD_TICK_HOURLY_THRESHOLD,
                    )
        self._bad_tick_count.clear()
