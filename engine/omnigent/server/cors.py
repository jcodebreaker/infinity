"""CORS for browser clients on other origins (``OMNIGENT_CORS_ALLOWED_ORIGINS``).

Off unless an allow-list is configured. Entries are exact origins (``scheme://host[:port]``);
``*`` is refused, and credentials (cookies) are never allowed cross-origin: a third-party site
authenticates with ``Authorization: Bearer``, which needs no cookie.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

CORS_ALLOWED_ORIGINS_ENV = "OMNIGENT_CORS_ALLOWED_ORIGINS"

_ALLOWED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
_ALLOWED_HEADERS = ["Authorization", "Content-Type", "Last-Event-ID"]
_PREFLIGHT_MAX_AGE_SECONDS = 600


def cors_allowed_origins() -> list[str]:
    """The configured origins, validated.

    :returns: Exact origins, e.g. ``["https://app.example.com"]``; empty when unset.
    :raises RuntimeError: On ``*`` or an entry that is not a bare ``http(s)`` origin.
    """
    raw = os.environ.get(CORS_ALLOWED_ORIGINS_ENV, "")
    origins: list[str] = []
    for entry in (part.strip() for part in raw.split(",")):
        if not entry:
            continue
        parsed = urlparse(entry)
        if (
            "*" in entry
            or parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or parsed.username
        ):
            raise RuntimeError(
                f"{CORS_ALLOWED_ORIGINS_ENV} entries must be exact origins such as "
                f"https://app.example.com, got {entry!r}"
            )
        origins.append(entry.rstrip("/"))
    return origins


def install_cors(app: FastAPI) -> None:
    """Add the CORS middleware when an allow-list is configured (outermost, for preflights)."""
    origins = cors_allowed_origins()
    if not origins:
        return
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=_ALLOWED_METHODS,
        allow_headers=_ALLOWED_HEADERS,
        max_age=_PREFLIGHT_MAX_AGE_SECONDS,
    )
