"""Database tests run every migration on each fresh database."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def _sqlite_db_template() -> Path | None:
    """Keep the cold schema initialization path under test here."""
    return None
