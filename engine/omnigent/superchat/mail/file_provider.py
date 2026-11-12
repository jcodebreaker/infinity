"""The file mailbox: a markdown file standing in for an inbox, for development only (ADR 0039,
amended). The format is in the folder's docstring (``superchat/mail/__init__.py``).

Read whole on every poll (a dev file is small); nothing is ever written back. A section that
cannot be read (no ``From``, ``Date`` with a time zone or ``Thread``) is skipped with a warning.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.superchat.mail.types import Message, Thread, addresses, message_id

_logger = logging.getLogger(__name__)

#: Where the file is; unset (or empty) means no file mailbox: the mail source is off.
PATH_ENV = "OMNIGENT_DEV_MAILBOX"
#: Headers kept on a message besides the ones it has fields for (they mark bulk or auto mail).
KIND_HEADERS = frozenset(
    {"list-id", "list-unsubscribe", "precedence", "auto-submitted", "x-autoreply"}
)
_FIELDS = frozenset({"from", "to", "cc", "date", "subject", "thread"})
_HEADER = re.compile(r"^([A-Za-z][A-Za-z-]*):\s*(.*)$")
_SECTION = re.compile(r"^## ", re.MULTILINE)


def _front_matter(text: str) -> tuple[dict[str, str], str]:
    """``(keys, rest)`` of a file that may open with a ``---`` block of ``key: value`` lines."""
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    for end in range(1, len(lines)):
        if lines[end].strip() == "---":
            keys: dict[str, str] = {}
            for line in lines[1:end]:
                key, sep, value = line.partition(":")
                if sep and key.strip():
                    keys[key.strip().casefold()] = value.strip()
            return keys, "\n".join(lines[end + 1 :])
    return {}, text


def _section(block: str) -> Message | None:
    """One ``## `` section as a message: header lines, a blank line, then the body."""
    lines = block.split("\n")[1:]  # the ``## `` line is a label for the reader only
    fields: dict[str, str] = {}
    headers: dict[str, str] = {}
    at = 0
    while at < len(lines) and not lines[at].strip():
        at += 1
    while at < len(lines) and lines[at].strip():
        found = _HEADER.match(lines[at].strip())
        if found is None:
            break
        name, value = found.group(1).casefold(), found.group(2).strip()
        if name in _FIELDS:
            fields[name] = value
        elif name in KIND_HEADERS:
            headers[name] = value
        at += 1
    body = "\n".join(lines[at:]).strip()
    sender, thread, when = fields.get("from"), fields.get("thread"), fields.get("date")
    if not sender or not thread or not when:
        return None
    try:
        date = datetime.fromisoformat(when)
    except ValueError:
        return None
    if date.tzinfo is None:
        return None
    return Message(
        id=message_id(thread, date.isoformat(), sender),
        thread=thread,
        sender=sender,
        to=fields.get("to", ""),
        cc=fields.get("cc", ""),
        date=date,
        subject=fields.get("subject") or "(no subject)",
        body=body,
        headers=headers,
    )


def parse(text: str) -> tuple[dict[str, str], list[Message]]:
    """``(front matter, messages in file order)`` of a mailbox file."""
    keys, rest = _front_matter(text.replace("\r\n", "\n"))
    starts = [m.start() for m in _SECTION.finditer(rest)]
    messages: list[Message] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(rest)
        message = _section(rest[start:end])
        if message is None:
            label = rest[start:end].split("\n", 1)[0][3:].strip()
            _logger.warning("file mailbox: section %r skipped (From, Date, Thread)", label[:80])
            continue
        messages.append(message)
    return keys, messages


class FileMailbox:
    """A mailbox read from one markdown file."""

    name = "file"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _read(self) -> tuple[dict[str, str], list[Message]]:
        try:
            return parse(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _logger.warning("file mailbox: %s does not exist", self.path)
            return {}, []

    @property
    def owner(self) -> str:
        """The front matter's ``owner``, else the single local person."""
        return self._read()[0].get("owner") or RESERVED_USER_LOCAL

    @property
    def me(self) -> str | None:
        """The front matter's ``me`` address, lower-cased."""
        found = addresses(self._read()[0].get("me"))
        return found[0] if found else None

    def messages(self) -> list[Message]:
        """Every message in the file, oldest first."""
        return sorted(self._read()[1], key=lambda m: m.at)

    def thread(self, thread_id: str) -> Thread | None:
        """One thread's messages, oldest first; ``None`` when no message has that thread."""
        found = tuple(m for m in self.messages() if m.thread == thread_id)
        return Thread(thread_id, found) if found else None


def from_env(environ: Iterable[tuple[str, str]] | None = None) -> FileMailbox | None:
    """The file mailbox ``OMNIGENT_DEV_MAILBOX`` names, or ``None`` when it is unset."""
    env = dict(environ) if environ is not None else os.environ
    path = (env.get(PATH_ENV) or "").strip()
    return FileMailbox(path) if path else None
