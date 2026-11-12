"""Runner-side handler for the knowledge tools: runs in the host, answers from its index.

The Muse's tools are the qmd-style set in :mod:`omnigent.superchat.knowledge.tools`.
``files_ingest`` / ``files_thumbnail`` / ``files_reindex`` / ``files_find`` are never offered to a
model: the engine's relays call them (through the runner's ``/mcp/execute``) to read an
attachment before its message is sent, and to answer Superchat's Library badges and citation chips.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import asdict, replace
from datetime import date
from typing import Any

from omnigent.runtime.mcp_tool_result import encode_mcp_image_result
from omnigent.superchat._handler_http import HEX_ID_RE, error
from omnigent.superchat.feature import HandlerCtx
from omnigent.superchat.knowledge.db import FileRow
from omnigent.superchat.knowledge.indexer import DEFAULT_K, MAX_K, RERANK_CANDIDATES, AmbiguousFile
from omnigent.superchat.knowledge.recall import (
    MAX_QUERIES,
    SOURCES,
    ClaimSearch,
    RecallHit,
    journal_line,
    recall_with_reason,
)
from omnigent.superchat.knowledge.runtime import KnowledgeRuntime, configure, get_runtime
from omnigent.superchat.knowledge.tools import UPLOADS_PREFIX
from omnigent.superchat.memory.handlers import handle_memory_tool

#: Marks a ``files_search`` result so the transcript can show its pages as citation chips.
SEARCH_RESULT_TYPE = "file_search"
#: Search tool -> the index mode it runs in.
_SEARCH_MODES = {"files_search": "keyword", "files_vsearch": "vector", "files_query": "hybrid"}


def runtime_for(server_client: Any) -> KnowledgeRuntime:
    """The host's index service, started, pointed at the engine for the model proxy."""
    if server_client is not None:
        configure(str(server_client.base_url))
    runtime = get_runtime()
    runtime.start()
    return runtime


def _runtime(ctx: HandlerCtx) -> KnowledgeRuntime:
    return runtime_for(ctx.server_client)


async def handle_knowledge_tool(ctx: HandlerCtx, args: dict[str, Any]) -> str:
    """
    Run one knowledge tool against the host's index.

    :param ctx: The call context.
    :param args: Parsed tool arguments.
    :returns: Tool output JSON string (an image envelope for a page that has an image).
    """
    runtime = _runtime(ctx)
    try:
        if ctx.tool_name == "recall":
            return await _recall(runtime, ctx, args)
        if ctx.tool_name == "recall_get":
            return await _recall_get(runtime, ctx, args)
        return await asyncio.to_thread(_run, runtime, ctx.tool_name, args)
    except Exception as exc:  # noqa: BLE001
        return error(f"{ctx.tool_name} failed: {exc}")


def _run(runtime: KnowledgeRuntime, tool_name: str, args: dict[str, Any]) -> str:
    if tool_name in _SEARCH_MODES:
        return _search(runtime, tool_name, args)
    if tool_name == "files_get":
        return _get(runtime, args)
    if tool_name == "files_multi_get":
        return _multi_get(runtime, args)
    if tool_name == "files_ingest":
        return _ingest(runtime, args)
    if tool_name == "files_find":
        return _find(runtime, args)
    if tool_name == "files_read_page":
        return _read_page(runtime, args)
    if tool_name == "files_status":
        return json.dumps(runtime.indexer.status())
    if tool_name == "files_thumbnail":
        return _thumbnail(runtime, args)
    if tool_name == "files_reindex":
        queued = runtime.indexer.reindex()
        runtime.kick()
        return json.dumps({"queued": queued})
    return error(f"unknown knowledge tool: {tool_name}")


def _page_number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else None


def _search(runtime: KnowledgeRuntime, tool_name: str, args: dict[str, Any]) -> str:
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return error(f"{tool_name} requires a query")
    file_ids = args.get("file_ids")
    ids: list[str] | None = None
    if isinstance(file_ids, list) and file_ids:
        ids = [i for i in file_ids if isinstance(i, str) and HEX_ID_RE.match(i)]
        if not ids:
            return error("file_ids must be file ids from an earlier result")
    k = args.get("k")
    limit = max(1, min(k, MAX_K)) if isinstance(k, int) and not isinstance(k, bool) else DEFAULT_K
    found = runtime.indexer.search(
        query.strip(),
        mode=_SEARCH_MODES[tool_name],
        file_ids=ids,
        k=limit,
        rerank=tool_name == "files_query" and args.get("rerank") is not False,
    )
    return json.dumps({"type": SEARCH_RESULT_TYPE, **found})


def _row(runtime: KnowledgeRuntime, args: dict[str, Any], tool: str) -> FileRow | str:
    """The file a call names (path, name or file_id), or the error JSON to return."""
    file_id = args.get("file_id")
    # ``path`` takes a workspace path or a plain file name; a ``file_id`` that is no id is one too.
    path = next(
        (v for v in (args.get("path"), args.get("file_name"), args.get("file")) if v), None
    )
    if isinstance(file_id, str) and not HEX_ID_RE.match(file_id):
        path, file_id = path or file_id, None
    if isinstance(path, str) and HEX_ID_RE.match(path.strip()) and not file_id:
        path, file_id = None, path.strip()
    if not file_id and not (isinstance(path, str) and path.strip()):
        return error(f"{tool} requires the file's name or path (or a file_id)")
    try:
        row = runtime.indexer.resolve(file_id, path if isinstance(path, str) else None)
    except AmbiguousFile as exc:
        listed = ", ".join(exc.candidates)
        return error(f"More than one file matches '{exc.ref}': {listed}. Pass the full path.")
    if row is None:
        return error("That file isn't in your files index (yet)")
    return row


def _get(runtime: KnowledgeRuntime, args: dict[str, Any]) -> str:
    row = _row(runtime, args, "files_get")
    if isinstance(row, str):
        return row
    max_chars = args.get("max_chars")
    cap = max_chars if isinstance(max_chars, int) and not isinstance(max_chars, bool) else None
    indexer = runtime.indexer
    try:
        found = indexer.get(row, pages=args.get("pages"), max_chars=cap)
        if found is None:  # indexed before Markdown was kept: read it now
            indexer.ingest(row.path)
            found = indexer.get(row, pages=args.get("pages"), max_chars=cap)
    except (ValueError, OSError) as exc:
        return error(str(exc))
    if found is None:
        return error("That file has no readable text")
    return json.dumps(found)


def _multi_get(runtime: KnowledgeRuntime, args: dict[str, Any]) -> str:
    pattern = next((v for v in (args.get("glob"), args.get("pattern")) if isinstance(v, str)), "")
    if not pattern.strip():
        return error("files_multi_get requires a glob")
    max_chars = args.get("max_chars")
    cap = max_chars if isinstance(max_chars, int) and not isinstance(max_chars, bool) else None
    return json.dumps(runtime.indexer.multi_get(pattern.strip(), max_chars=cap))


def _ingest(runtime: KnowledgeRuntime, args: dict[str, Any]) -> str:
    path = args.get("path")
    if not isinstance(path, str) or not path.strip():
        return error("files_ingest requires a path")
    try:
        return json.dumps(runtime.indexer.ingest(path.strip(), within=UPLOADS_PREFIX))
    except FileNotFoundError:
        return error("That file isn't in your workspace")
    except ValueError as exc:
        return error(str(exc))


def _find(runtime: KnowledgeRuntime, args: dict[str, Any]) -> str:
    digest = args.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        return error("files_find requires a sha256")
    return json.dumps(runtime.indexer.find_upload(digest) or {})


def _read_page(runtime: KnowledgeRuntime, args: dict[str, Any]) -> str:
    page = _page_number(args.get("page"))
    if page is None:
        return error("files_read_page requires a page number from 1")
    row = _row(runtime, args, "files_read_page")
    if isinstance(row, str):
        return row
    found = runtime.indexer.page(row, page)
    if found is None:
        return error("Page not found")
    image = found.pop("image_base64", None)
    mime = found.pop("image_mime", None)
    if not image or not mime:
        return json.dumps(found)
    return encode_mcp_image_result(
        [
            {"type": "text", "text": json.dumps(found)},
            {"type": "image", "mimeType": mime, "data": image},
        ],
        is_error=False,
    )


def _thumbnail(runtime: KnowledgeRuntime, args: dict[str, Any]) -> str:
    file_id, page = args.get("file_id"), _page_number(args.get("page"))
    if not (isinstance(file_id, str) and HEX_ID_RE.match(file_id)) or page is None:
        return error("files_thumbnail requires file_id and page")
    data = runtime.indexer.thumbnail(file_id, page)
    if data is None:
        return error("no thumbnail")
    return json.dumps(
        {"image_base64": base64.b64encode(data).decode(), "image_mime": "image/webp"}
    )


# ---------------------------------------------------------------------- recall (ADR 0016)

#: ``recall_get`` reads at most this many ids per call.
MAX_READ_IDS = 10
_FILTERS = ("project", "file", "chat", "since", "until")


async def _recall(runtime: KnowledgeRuntime, ctx: HandlerCtx, args: dict[str, Any]) -> str:
    """``recall``: Recall's search over the asked sources and filters, then the model rerank."""
    asked = args.get("queries")
    asked = [asked] if isinstance(asked, str) else asked if isinstance(asked, list) else []
    queries = [q.strip() for q in [*asked, args.get("query")] if isinstance(q, str) and q.strip()]
    named = args.get("sources")
    named = [named] if isinstance(named, str) else named
    sources = set(SOURCES)
    if isinstance(named, list) and named and "all" not in named:
        sources = {s for s in named if isinstance(s, str)}
        if not sources <= SOURCES:
            return error("sources are files, journal, conversation, memory or all")
    filters = {k: v.strip() for k in _FILTERS if isinstance(v := args.get(k), str) and v.strip()}
    for key in ("since", "until"):
        try:
            if key in filters:
                date.fromisoformat(filters[key][:10])
        except ValueError:
            return error(f"{key} must be a date like 2026-09-14")
    if len(queries) > MAX_QUERIES:
        return error(f"recall takes at most {MAX_QUERIES} queries")
    k = args.get("k")
    limit = max(1, min(k, MAX_K)) if isinstance(k, int) and not isinstance(k, bool) else DEFAULT_K
    rerank = args.get("rerank") is not False and bool(queries) and "file" not in filters
    indexer = runtime.indexer
    try:
        hits, reason = await recall_with_reason(
            queries,
            sources=sources,
            recency=args.get("recency") is not False,
            limit=max(limit, RERANK_CANDIDATES) if rerank else limit,
            exclude_chat_after=await _current_epoch(ctx),
            indexer=indexer,
            claims=_claims(ctx),
            **filters,
        )
    except ValueError as exc:
        return error(str(exc))
    out: dict[str, Any] = {
        "type": "recall",
        "queries": queries,
        "sources": sorted(sources),
        **filters,
        "mode": "keyword" if reason else "hybrid",
        "keyword_only_reason": reason,
        "files_indexing": indexer.db.pending_count(),
    }
    results = [_hit_view(hit) for hit in hits]
    if rerank:
        ranked, verdict = await asyncio.to_thread(
            indexer.rerank_hits,
            "; ".join(queries),
            [{**r, "passage": r["snippet"]} for r in results],
        )
        results = [{k: v for k, v in r.items() if k != "passage"} for r in ranked]
        out.update(verdict)
    out["results"] = results[:limit]
    return json.dumps(out)


async def _current_epoch(ctx: HandlerCtx) -> dict[str, str] | None:
    """The calling chat's current epoch (:func:`current_epoch`), ``None`` outside a chat."""
    if ctx.server_client is None or not ctx.conversation_id:
        return None
    return await current_epoch(ctx.server_client, ctx.conversation_id)


async def current_epoch(
    server_client: Any, conversation_id: str, checkpoint: dict[str, Any] | None = None
) -> dict[str, str]:
    """A chat's current epoch, which the model already has, as recall's exclusion map: its turns
    after the view start the history load wrote (ADR 0031; a fold moves the anchor but not what
    a warm session holds), else after the latest checkpoint's anchor (*checkpoint* when already
    read), or all of them with no checkpoint."""
    from omnigent.context.rollover import VIEW_START_LABEL
    from omnigent.superchat.rollover import fetch_latest_checkpoint

    try:
        resp = await server_client.get(f"/v1/sessions/{conversation_id}/labels", timeout=10.0)
        labels = resp.json().get("labels") if resp.status_code == 200 else None
    except Exception:  # noqa: BLE001 - an unread label falls back to the checkpoint
        labels = None
    if isinstance(labels, dict) and isinstance(labels.get(VIEW_START_LABEL), str):
        return {conversation_id: labels[VIEW_START_LABEL]}
    if checkpoint is None:
        try:
            checkpoint = await fetch_latest_checkpoint(
                server_client, conversation_id, skip_placeholders=True
            )
        except Exception:  # noqa: BLE001 - no checkpoint known is "the whole chat is this epoch"
            checkpoint = None
    return {conversation_id: (checkpoint or {}).get("last_item_id") or ""}


def _hit_view(hit: RecallHit) -> dict[str, Any]:
    """A hit as the model reads it: what is set, nothing empty."""
    return {k: v for k, v in asdict(hit).items() if v not in (None, "", [], {})}


def _claims(ctx: HandlerCtx) -> ClaimSearch | None:
    """Claim search through the engine route ``memory_search`` uses (needs the server)."""
    if ctx.server_client is None or ctx.conversation_id is None:
        return None
    search_ctx = replace(ctx, tool_name="memory_search")

    async def search(query: str, limit: int) -> list[dict[str, Any]]:
        found = json.loads(await handle_memory_tool(search_ctx, {"query": query, "limit": limit}))
        results = found.get("results") if isinstance(found, dict) else None
        return results if isinstance(results, list) else []

    return search


async def _recall_get(runtime: KnowledgeRuntime, ctx: HandlerCtx, args: dict[str, Any]) -> str:
    """``recall_get``: read hits by id, each through the reader its prefix names."""
    ids = args.get("ids")
    ids = [ids] if isinstance(ids, str) else ids if isinstance(ids, list) else []
    ids = [i.strip() for i in [*ids, args.get("id")] if isinstance(i, str) and i.strip()]
    if not ids:
        return error("recall_get requires an id (or ids) from recall")
    if len(ids) > MAX_READ_IDS:
        return error(f"recall_get reads at most {MAX_READ_IDS} ids")
    found = [await _read_one(runtime, ctx, read_id) for read_id in dict.fromkeys(ids)]
    return json.dumps({"type": "recall_get", "results": found})


async def _read_one(runtime: KnowledgeRuntime, ctx: HandlerCtx, read_id: str) -> dict[str, Any]:
    prefix, _, rest = read_id.partition("_")
    try:
        if prefix == "i" and rest:
            body = await _read_turn(runtime, ctx, rest)
        elif prefix == "ep" and rest.isdigit():
            body = await asyncio.to_thread(_read_journal, runtime, int(rest))
        elif prefix == "f" and rest:
            file_id, _, pages = rest.partition("@")
            body = json.loads(
                await asyncio.to_thread(
                    _get, runtime, {"file_id": file_id, "pages": pages or None}
                )
            )
        elif prefix == "c" and rest:
            explain = replace(ctx, tool_name="memory_explain")
            body = json.loads(await handle_memory_tool(explain, {"claim_id": rest}))
        else:
            body = {"error": "unknown id: ids start with i_, ep_, f_ or c_"}
    except Exception as exc:  # noqa: BLE001 - one bad id never fails the others
        body = {"error": f"could not read it: {exc}"}
    return {"id": read_id, **body}


def _read_journal(runtime: KnowledgeRuntime, passage_id: int) -> dict[str, Any]:
    """A journal passage from the index, with its first line's number and date."""
    db = runtime.indexer.db
    chunk = db.chunks_by_ids([passage_id]).get(passage_id)
    row = db.get_file(chunk.file_id) if chunk else None
    if chunk is None or row is None or not row.path.startswith("memory/"):
        return {"error": "no journal passage with that id"}
    _, line, day = journal_line(runtime.indexer, row, chunk, set())
    refs = db.refs_of([chunk.id]).get(chunk.id, {})
    return {
        "source": "journal",
        "path": row.path,
        "line": line,
        "date": day,
        "text": chunk.text,
        **({"refs": refs} if refs else {}),
    }


async def _read_turn(runtime: KnowledgeRuntime, ctx: HandlerCtx, item_id: str) -> dict[str, Any]:
    """A whole turn through ``session_history read``: paged back from the next turn's start."""
    db = runtime.indexer.db
    chunk_id = db.turn_chunks([item_id]).get(item_id)
    turn = db.turn_meta([chunk_id]).get(chunk_id) if chunk_id is not None else None
    if chunk_id is None or turn is None:
        return {"error": "no turn with that id"}
    view = {"source": "conversation", "chat_id": turn["chat_id"], "date": turn["date"][:10]}
    if ctx.server_client is None or ctx.conversation_id is None:
        chunk = db.chunks_by_ids([chunk_id])[chunk_id]
        return {**view, "text": chunk.text, "note": "the turn as indexed"}
    from omnigent.runner.tool_dispatch import _execute_session_history_tool
    from omnigent.tools.builtins.session_history import project_api_item

    cursor, after = None, item_id
    for _ in range(5):  # a turn longer than 500 items is read from its newest part
        resp = await ctx.server_client.get(
            f"/v1/sessions/{turn['chat_id']}/items",
            params={"after": after, "order": "asc", "limit": 100},
            timeout=30.0,
        )
        resp.raise_for_status()
        page = resp.json()
        items = [project_api_item(raw) for raw in page.get("data", [])]
        cursor = next(
            (i["id"] for i in items if i.get("type") == "text" and i.get("role") == "user"), None
        )
        if cursor or not page.get("has_more") or not items:
            break
        after = items[-1]["id"]
    read = json.loads(
        await _execute_session_history_tool(
            {"action": "read", "chat_id": turn["chat_id"], "cursor": cursor, "limit": 1},
            conversation_id=ctx.conversation_id,
            server_client=ctx.server_client,
        )
    )
    turns = read.get("turns") or []
    messages = turns[0]["messages"] if turns else []
    if not messages or messages[0].get("id") != item_id:
        return {**view, "error": read.get("error") or "that turn is no longer in the chat"}
    return {**view, "messages": messages}
