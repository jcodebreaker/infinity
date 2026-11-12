"""``POST .../read``: move a viewer's read baseline for one chat.

Read-state is the per-user store ``PUT .../read-state`` and the session list already use
(in memory, per workspace; it resets with the server).
"""

from __future__ import annotations

import time

from omnigent.db.db_models import InvalidUuidError, uuid_to_bytes
from omnigent.stores import ConversationStore


def mark_read(
    conv_store: ConversationStore,
    user_id: str | None,
    session_id: str,
    item_id: str | None,
) -> int | None:
    """Mark *session_id* read for *user_id*, up to *item_id* (default: now).

    The baseline only moves forward; the explicit unread flag is cleared.

    :returns: The new ``last_read_at``, or ``None`` when *item_id* is not in the session.
    """
    from omnigent.server.routes._sessions.helpers import _read_state_entry, _set_read_state

    if item_id is not None:
        try:
            uuid_to_bytes(item_id)
        except InvalidUuidError:
            return None
        item = conv_store.get_item(session_id, item_id)
        if item is None:
            return None
        at = item.created_at
    else:
        at = int(time.time())
    last_seen, _ = _read_state_entry(user_id, session_id)
    read_at = max(at, last_seen or 0)
    _set_read_state(user_id, session_id, read_at, False)
    return read_at
