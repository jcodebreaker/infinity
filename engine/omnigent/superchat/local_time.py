"""Per-turn local date/time line for a superside-chat session.

The model has no clock of its own, and the framework's other date stamps are
UTC. The runner prefixes each superside-chat turn with this one line, rendered
in the owner's preference timezone (``/v1/me/proactivity``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx


async def fetch_timezone(server_client: Any, *, timeout: float = 10.0) -> str | None:
    """The owner's preference timezone (``/v1/me/proactivity``), ``None`` when unknown."""
    try:
        if server_client is not None:
            resp = await server_client.get("/v1/me/proactivity", timeout=timeout)
            if resp.status_code == 200:
                found = resp.json().get("timezone")
                return found if isinstance(found, str) and found else None
    except (httpx.HTTPError, ValueError, AttributeError):
        pass
    return None


def _zone(timezone: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(timezone) if timezone else ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def local_date(timezone: str | None, *, now: datetime | None = None) -> str:
    """The person's current local date, ``YYYY-MM-DD`` (UTC when the zone is unknown)."""
    return f"{(now or datetime.now(UTC)).astimezone(_zone(timezone)):%Y-%m-%d}"


#: How the local-time line starts; it ends with ``]`` on the same line.
LOCAL_TIME_OPEN = "[Current local time for the person: "


def local_time_block(timezone: str | None, *, now: datetime | None = None) -> str:
    """Render the person's current local time, e.g.
    ``[Current local time for the person: Sunday, October 4, 2026, 9:12 AM
    (America/Toronto, UTC-04:00)]``.

    :param timezone: IANA zone name; unknown or empty falls back to UTC.
    :param now: Instant to render (tests); defaults to the current time.
    """
    zone = _zone(timezone)
    local = (now or datetime.now(UTC)).astimezone(zone)
    offset = local.strftime("%z")
    sign = "\u2212" if offset.startswith("-") else "+"
    clock = f"{local:%A, %B} {local.day}, {local.year}, {local.hour % 12 or 12}:{local:%M %p}"
    return f"{LOCAL_TIME_OPEN}{clock} ({zone.key}, UTC{sign}{offset[1:3]}:{offset[3:5]})]"
