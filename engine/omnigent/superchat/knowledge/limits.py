"""Limits and places the host's file index and the engine's relays to it agree on."""

from __future__ import annotations

#: Where composer uploads land in the workspace; the only place an attachment is read from.
UPLOADS_PREFIX = "your_files/uploads/"
#: Reading one big file (parse, embed, thumbnails) is one awaited call; the relay gives up after
#: this, and the background indexer never holds an ingest back for longer.
INGEST_TIMEOUT_S = 300.0
