"""
TradeOS — system_events DB writer.

write_system_event() — inserts a row into the system_events table.

Rules:
  - Never raises — catches all DB exceptions and logs ERROR.
  - Captures kill_switch_level snapshot at write time.
  - Serialises detail dict to JSONB.
"""
from __future__ import annotations

import json
import structlog
from datetime import datetime
from typing import Optional

import pytz

log = structlog.get_logger()
IST = pytz.timezone("Asia/Kolkata")


async def write_system_event(
    pool,  # asyncpg.Pool
    event_type: str,
    level: str,
    shared_state: dict,
    detail: Optional[dict] = None,
) -> None:
    """
    Insert a row into the system_events table.

    Args:
        pool:        asyncpg connection pool.
        event_type:  Event type string (e.g. 'KILL_SWITCH_L1', 'RECONCILIATION_COMPLETE').
        level:       Log level string ('INFO', 'WARNING', 'CRITICAL').
        shared_state: D6 shared state dict — kill_switch_level snapshot taken here.
        detail:      Optional dict of event-specific data (stored as JSONB).
    """
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO system_events
                    (session_date, event_time, event_type, level, detail, kill_switch_level)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6)
                """,
                datetime.now(IST).date(),
                datetime.now(IST),
                event_type,
                level,
                json.dumps(detail or {}),
                shared_state.get("kill_switch_level", 0),
            )
        log.debug(
            "system_event_written",
            event_type=event_type,
            level=level,
        )
    except Exception as exc:
        log.error(
            "write_system_event_failed",
            event_type=event_type,
            level=level,
            error=str(exc),
        )
