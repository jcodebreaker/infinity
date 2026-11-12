"""Redaction of the secrets the engine itself knows, applied to what clients read.

A client must never see a deployment secret, whichever route it read: the transcript, the raw
items, the Activity Feed, the Feed or the asks inbox. The values come from two places:

* **This server's environment.** Every variable whose *name* marks it as a secret
  (``*_API_KEY``, ``*_KEY``, ``*TOKEN``, ``*SECRET*``, ``*PASSWORD*``, ``*CREDENTIAL*``),
  which covers the configured provider keys (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, …),
  the vault key and every ``host.runner_env`` / passthrough credential forwarded to
  runners. :data:`EXTRA_NAMES_ENV` adds names the pattern misses (comma-separated).
* **The vault.** The saved passwords of the people a response concerns (the session owner
  and the caller), decrypted in-process only and never logged.

Only values of :data:`MIN_SECRET_LENGTH` characters or more are redacted: a shorter value
(``true``, a port) would blank ordinary text. Each value also matches in its JSON-escaped
form, so a secret inside a tool call's ``arguments`` string is caught too.

Matching is one compiled alternation, longest value first, rebuilt only when the set of
values changes: the environment is re-read on each call (a regex over its names), a person's
vault values are cached for :data:`VAULT_CACHE_SECONDS` and dropped as soon as the vault
changes on this replica (:func:`invalidate_vault`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections.abc import Iterable
from typing import Any

_logger = logging.getLogger(__name__)

#: What a redacted value is replaced with (the same token Superchat's client used).
REDACTED = "[redacted]"
#: Shorter values are never redacted.
MIN_SECRET_LENGTH = 8
#: Comma-separated extra environment variable NAMES whose values are secrets.
EXTRA_NAMES_ENV = "OMNIGENT_REDACT_ENV"
#: How long a person's decrypted vault values are reused before a re-read (another replica
#: may have changed them; this replica's own changes invalidate at once).
VAULT_CACHE_SECONDS = 60.0

_SECRET_NAME = re.compile(r"(_KEY|TOKEN|CREDENTIALS?)$|SECRET|PASSW(OR)?D|API_KEY|_TOKEN_")


def is_secret_env_name(name: str) -> bool:
    """Whether an environment variable's name marks its value as a secret."""
    return bool(_SECRET_NAME.search(name.upper()))


def env_secret_values(environ: dict[str, str] | None = None) -> frozenset[str]:
    """The values of this process's secret-named environment variables (8+ characters)."""
    env = os.environ if environ is None else environ
    extra = {n.strip() for n in env.get(EXTRA_NAMES_ENV, "").split(",") if n.strip()}
    return frozenset(
        value
        for name, value in env.items()
        if (name in extra or is_secret_env_name(name)) and len(value) >= MIN_SECRET_LENGTH
    )


class Redactor:
    """Replaces every known secret value in text with :data:`REDACTED`."""

    def __init__(self, values: Iterable[str]) -> None:
        """:param values: Secret values; short ones are ignored."""
        forms: set[str] = set()
        for value in values:
            if not isinstance(value, str) or len(value) < MIN_SECRET_LENGTH:
                continue
            forms.add(value)
            escaped = json.dumps(value)[1:-1]
            if escaped != value:
                forms.add(escaped)
        ordered = sorted(forms, key=len, reverse=True)
        self._pattern = re.compile("|".join(map(re.escape, ordered))) if ordered else None

    @property
    def active(self) -> bool:
        """Whether there is anything to redact."""
        return self._pattern is not None

    def text(self, value: str) -> str:
        """*value* with every secret replaced."""
        if self._pattern is None or not value:
            return value
        return self._pattern.sub(REDACTED, value)

    def deep(self, value: Any) -> Any:
        """A copy of a JSON-shaped *value* with every string (keys included) redacted.

        Numbers, booleans and ``None`` pass through; the input is never mutated.
        """
        if self._pattern is None:
            return value
        return self._deep(value)

    def _deep(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {
                (self.text(k) if isinstance(k, str) else k): self._deep(v)
                for k, v in value.items()
            }
        if isinstance(value, list | tuple):
            return [self._deep(v) for v in value]
        return value


_EMPTY = Redactor(())
_lock = threading.Lock()
#: owner -> (read at, decrypted values)
_vault_cache: dict[str | None, tuple[float, frozenset[str]]] = {}
#: Every value redacted together -> its compiled redactor; bounded.
_compiled: dict[frozenset[str], Redactor] = {}
_COMPILED_MAX = 64


def invalidate_vault(user_id: str | None = None) -> None:
    """Forget cached vault values: one owner's, or everyone's when *user_id* is omitted."""
    with _lock:
        if user_id is None:
            _vault_cache.clear()
        else:
            _vault_cache.pop(user_id, None)


def _vault_values(vault_store: Any, owner: str | None) -> frozenset[str]:
    now = time.monotonic()
    with _lock:
        cached = _vault_cache.get(owner)
    if cached is not None and now - cached[0] < VAULT_CACHE_SECONDS:
        return cached[1]
    try:
        values = frozenset(vault_store.secret_values(user_id=owner))
    except Exception:  # a vault that is off or unreadable has nothing to add
        _logger.debug("redaction: vault values unavailable", exc_info=True)
        values = frozenset()
    with _lock:
        _vault_cache[owner] = (now, values)
    return values


def _vault_owner(user_id: str | None) -> str | None:
    """The vault's owner key for a user id (local/single-user is ``None``, as the vault does)."""
    from omnigent.server.auth import RESERVED_USER_LOCAL

    return None if user_id in (None, RESERVED_USER_LOCAL) else user_id


def redactor_for(vault_store: Any, user_ids: Iterable[str | None] = ()) -> Redactor:
    """The redactor for a response concerning *user_ids*.

    :param vault_store: The server's ``VaultStore`` (``app.state.vault_store``), or ``None``
        when the vault feature is off.
    :param user_ids: Whose vault values to include: the session owner and the caller.
    :returns: A redactor over the environment's secrets plus those vault values.
    """
    values = set(env_secret_values())
    if vault_store is not None:
        for owner in {_vault_owner(u) for u in user_ids}:
            values |= _vault_values(vault_store, owner)
    if not values:
        return _EMPTY
    key = frozenset(values)
    with _lock:
        found = _compiled.get(key)
    if found is not None:
        return found
    redactor = Redactor(key)
    with _lock:
        if len(_compiled) >= _COMPILED_MAX:
            _compiled.clear()
        _compiled[key] = redactor
    return redactor


def request_redactor(request: Any, *user_ids: str | None) -> Redactor:
    """:func:`redactor_for` with the vault store taken from *request*'s app."""
    vault_store = getattr(request.app.state, "vault_store", None)
    return redactor_for(vault_store, user_ids)


async def session_redactor(
    request: Any,
    conversation_store: Any,
    session_id: str,
    user_id: str | None,
) -> Redactor:
    """The redactor for a response about *session_id*: its owner's and the caller's values."""
    owner = await asyncio.to_thread(conversation_store.get_session_owner, session_id)
    return request_redactor(request, owner, user_id)
