"""Knowledge: a person's own file search (a small RAG over the files in their host).

The host's workspace is the one copy of a person's files, and the index lives there too. The
engine's side holds no file data: ``tools`` / ``handlers`` register the Muse's qmd-style
``files_*`` tools (the handler runs in the host), and ``routes`` relays ingest (an attachment
read before its message is sent), status, search and page thumbnails to it for Superchat.
Embeddings and the search rerank are made through the engine's model proxy (``models``), so no key
enters the host.

The host side keeps parsing, passages, thumbnails and the index on the home volume
(``~/.superchat/knowledge``): ``chunking`` / ``parse`` / ``pdf`` (text and renders, no ML), ``db``
(SQLite: FTS5 BM25 plus sqlite-vec), ``embedder`` (the model proxy client), ``indexer``
(reconcile, passes, search), ``runtime`` (the background thread and the handlers' way in).

It sits on ``artifacts`` only to tell a Library file from a plain one in the answers (matched by
the workspace path the artifact was saved from).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from omnigent.context.labels import is_superside_chat
from omnigent.superchat.feature import Feature, InstallDeps, MessageBlock, ToolManagerCtx
from omnigent.superchat.knowledge.handlers import handle_knowledge_tool
from omnigent.superchat.knowledge.tools import KNOWLEDGE_TOOL_NAMES

if TYPE_CHECKING:
    from fastapi import FastAPI

    from omnigent.superchat.feature import HandlerCtx
    from omnigent.tools.base import Tool

#: Handlers the engine's relays call; never offered to a model.
_INTERNAL_OPS = ("files_ingest", "files_thumbnail", "files_reindex", "files_find")


def _tools(labels: Mapping[str, str] | None, _ctx: ToolManagerCtx) -> list[Tool]:
    """The Super Chat and its Helpers search the person's files."""
    from omnigent.superchat.knowledge import tools

    if not is_superside_chat(labels):
        return []
    return [
        tools.FilesSearchTool(),
        tools.FilesVsearchTool(),
        tools.FilesQueryTool(),
        tools.FilesGetTool(),
        tools.FilesMultiGetTool(),
        tools.FilesReadPageTool(),
        tools.FilesStatusTool(),
        tools.RecallTool(),
        tools.RecallGetTool(),
    ]


async def _message_prefix(
    server_client: Any, _conversation_id: str, texts: Sequence[str]
) -> list[MessageBlock]:
    """The framed text of the files the turn's message attaches and its images as image blocks
    (built here, never stored)."""
    from omnigent.superchat.knowledge.attachment_context import (
        attachment_context_blocks,
        image_blocks,
        image_references,
        references,
    )
    from omnigent.superchat.knowledge.handlers import runtime_for

    if not references(texts) and not image_references(texts):
        return []
    indexer = runtime_for(server_client).indexer
    framed, images = await asyncio.gather(
        attachment_context_blocks(indexer, texts), image_blocks(indexer, texts)
    )
    return [*framed, *images]


def _needs_own_turn(text: str) -> bool:
    """A message attaching a file needs its turn start, where the file's text is put in front."""
    from omnigent.superchat.knowledge.attachment_context import REFERENCE_LINE

    return bool(REFERENCE_LINE.search(text.replace("\r\n", "\n")))


def _install(app: FastAPI, deps: InstallDeps) -> None:
    from omnigent.runtime import get_artifact_store
    from omnigent.superchat.artifacts import SqlAlchemyArtifactStore
    from omnigent.superchat.knowledge.routes import create_knowledge_router

    if deps.scheduled_task_store is None or deps.conversation_store is None:
        return
    location = deps.scheduled_task_store.storage_location
    app.include_router(
        create_knowledge_router(
            conversation_store=deps.conversation_store,
            artifact_store=SqlAlchemyArtifactStore(location, get_artifact_store),
            runner_router=deps.runner_router,
            permission_store=deps.permission_store,
            auth_provider=deps.auth_provider,
        ),
        prefix="/v1",
        tags=["knowledge"],
    )


def _on_result(ctx: HandlerCtx, output: str) -> None:
    """A file the Muse saved as an artifact is searchable from the workspace it was saved from."""
    if ctx.tool_name != "artifact_save":
        return
    try:
        saved = json.loads(output)
    except ValueError:
        return
    path = saved.get("source_path") if isinstance(saved, dict) else None
    if not isinstance(path, str) or not path:
        return
    from omnigent.superchat.knowledge.runtime import notify_saved

    notify_saved(path)


FEATURE = Feature(
    name="knowledge",
    tools=_tools,
    handlers=dict.fromkeys((*KNOWLEDGE_TOOL_NAMES, *_INTERNAL_OPS), handle_knowledge_tool),
    relay_ops=frozenset(_INTERNAL_OPS),
    on_result=_on_result,
    message_prefix=_message_prefix,
    needs_own_turn=_needs_own_turn,
    install=_install,
)
