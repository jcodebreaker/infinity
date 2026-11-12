"""The outbox (ADR 0036): every proactive message writes one row; channels read after a cursor.

Layout: ``cut`` (the row's title and line, and the source's label, pure), ``store`` (the
``outbox_items``, ``outbox_cursors`` and ``outbox_feedback`` tables), ``record`` (where a
proactive message gets its label and row), ``routes`` (``/v1/me/outbox``). The label is read
back onto the transcript's messages as ``proactive``; the person's mutes are offered to the
Lookout through :func:`omnigent.superchat.feature.list_mutes`; a message engine code posts
itself (the Lookout's, the brief's) gets its row through
:func:`omnigent.superchat.feature.record_proactive`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from omnigent.superchat.feature import Feature, InstallDeps

if TYPE_CHECKING:
    from fastapi import FastAPI


def _install(app: FastAPI, deps: InstallDeps) -> None:
    from omnigent.server.routes._sessions.helpers import ON_MESSAGE_STORED
    from omnigent.server.routes._sessions.orchestration import (
        ON_MESSAGE_CONTEXT,
        ON_SESSION_DELETED,
    )
    from omnigent.superchat.feature import (
        TRANSCRIPT_FIELDS,
        register_mutes_reader,
        register_proactive_reader,
        register_proactive_recorder,
        register_proactive_unseen,
    )
    from omnigent.superchat.outbox.record import Outbox
    from omnigent.superchat.outbox.routes import create_outbox_router
    from omnigent.superchat.outbox.store import SqlAlchemyOutboxStore, owner_key

    if deps.storage_location is None:
        return
    store = SqlAlchemyOutboxStore(deps.storage_location)
    outbox = Outbox(store, deps.conversation_store, deps.scheduled_task_store)
    app.state.outbox_store = store
    ON_MESSAGE_CONTEXT["outbox"] = outbox.notice
    ON_MESSAGE_STORED["outbox"] = outbox.message
    ON_SESSION_DELETED["outbox"] = outbox.forget

    def proactive(conversation_id: str, messages: list[dict[str, Any]]) -> None:
        ids = [str(m["id"]) for m in messages if m.get("role") == "assistant"]
        labels = store.labels(conversation_id, ids)
        for message in messages:
            if (label := labels.get(str(message["id"]))) is not None:
                message["proactive"] = label

    TRANSCRIPT_FIELDS["outbox"] = proactive
    register_mutes_reader(store.list_mutes)
    # The rhythm (ADR 0038) reads which messages were proactive, and when, through core.
    register_proactive_reader(store.proactive_between, store.proactive_one)
    register_proactive_recorder(outbox.record)
    # Home leads with prepared work the person has not opened yet (topics, through core).
    register_proactive_unseen(lambda owner, item_id: store.unseen(owner_key(owner), item_id))
    app.include_router(
        create_outbox_router(store, auth_provider=deps.auth_provider),
        prefix="/v1",
        tags=["outbox"],
    )


FEATURE = Feature(name="outbox", tools=lambda _labels, _ctx: [], install=_install)
