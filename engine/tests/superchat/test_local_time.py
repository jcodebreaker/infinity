from datetime import UTC, datetime

from omnigent.superchat.local_time import local_time_block

_NOW = datetime(2026, 10, 4, 13, 12, tzinfo=UTC)


def test_renders_in_owner_zone() -> None:
    assert local_time_block("America/Toronto", now=_NOW) == (
        "[Current local time for the person: Sunday, October 4, 2026, 9:12 AM "
        "(America/Toronto, UTC\u221204:00)]"
    )


def test_unknown_zone_falls_back_to_utc() -> None:
    assert "1:12 PM (UTC, UTC+00:00)" in local_time_block("Nope/Zone", now=_NOW)
    assert "(UTC, UTC+00:00)" in local_time_block(None, now=_NOW)


def test_noon_is_pm_and_midnight_is_am() -> None:
    noon = datetime(2026, 10, 4, 16, 24, tzinfo=UTC)
    assert "12:24 PM" in local_time_block("America/Toronto", now=noon)
    midnight = datetime(2026, 10, 4, 4, 24, tzinfo=UTC)
    assert "12:24 AM" in local_time_block("America/Toronto", now=midnight)
