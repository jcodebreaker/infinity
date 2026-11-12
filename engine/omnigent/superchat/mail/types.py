"""What a mail provider hands the mail source, whatever the service behind it (ADR 0039). Pure."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

_ADDRESS = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def addresses(value: str | None) -> tuple[str, ...]:
    """The mail addresses in a header value, lower-cased, in order."""
    return tuple(a.casefold() for a in _ADDRESS.findall(value or ""))


def message_id(thread: str, date: str, sender: str) -> str:
    """A message's id: a hash of its thread, date and sender (stable across reads)."""
    raw = f"{thread}\0{date}\0{sender}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


@dataclass(frozen=True)
class Message:
    """One mail.

    :param id: The provider's id of the message.
    :param thread: The thread (conversation) it belongs to.
    :param sender: ``From``, as written (``"Name <address>"``).
    :param to: ``To`` addresses as written.
    :param cc: ``Cc`` addresses as written.
    :param date: When it was sent (time-zone aware).
    :param subject: Its subject.
    :param body: Its plain-text body.
    :param headers: Other headers that say what kind of mail it is (``list-id``,
        ``precedence``, ``auto-submitted`` ...), names lower-cased.
    """

    id: str
    thread: str
    sender: str
    to: str
    cc: str
    date: datetime
    subject: str
    body: str
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def at(self) -> int:
        """Epoch seconds it was sent."""
        return int(self.date.timestamp())

    def from_address(self) -> str | None:
        """The sender's address, lower-cased."""
        found = addresses(self.sender)
        return found[0] if found else None

    def recipients(self) -> tuple[str, ...]:
        """Every ``To`` and ``Cc`` address, lower-cased."""
        return addresses(self.to) + addresses(self.cc)


@dataclass(frozen=True)
class Thread:
    """A thread's messages, oldest first."""

    id: str
    messages: tuple[Message, ...]

    @property
    def subject(self) -> str:
        """The first message's subject."""
        return self.messages[0].subject if self.messages else ""


class Provider(Protocol):
    """A mailbox the source reads (the file mailbox now; Microsoft 365 and Google later).

    ``owner`` is the person the mailbox belongs to, ``me`` their own address (their messages
    are how the source tells a thread they wrote in), ``messages()`` every message it can see
    now, ``thread(id)`` one thread (``None`` when unknown).
    """

    name: str

    @property
    def owner(self) -> str: ...

    @property
    def me(self) -> str | None: ...

    def messages(self) -> list[Message]: ...

    def thread(self, thread_id: str) -> Thread | None: ...
