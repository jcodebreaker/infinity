"""Reading a workspace file without following a link swapped in after the path was checked."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import BinaryIO

_CHUNK = 1024 * 1024


def open_nofollow(path: Path) -> BinaryIO:
    """Open *path* for reading; fails (``OSError``) when its last part is a symlink.

    The indexer resolves a path before it reads it; ``O_NOFOLLOW`` makes sure the file read is the
    one that was checked, not a link put there in between.
    """
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    return os.fdopen(fd, "rb")


def read_bytes(path: Path) -> bytes:
    """The whole file, opened with :func:`open_nofollow`."""
    with open_nofollow(path) as handle:
        return handle.read()


def sha256_of(path: Path) -> str:
    """The SHA-256 (hex) of the file, read as a stream."""
    digest = hashlib.sha256()
    with open_nofollow(path) as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()
