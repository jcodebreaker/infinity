"""Runner-side handlers for the artifact tools.

``artifact_save`` runs where the host's files are (the runner), reads the file from the
workspace and uploads it to the server; list/delete proxy the REST endpoints.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

from omnigent.superchat._handler_http import HEX_ID_RE, error, finish, resolve_caller
from omnigent.superchat.artifact_kinds import (
    KIND_MIME,
    MAX_ARTIFACT_BYTES,
    is_deck_name,
    kind_for_name,
)
from omnigent.superchat.artifacts.look import is_page, saved_look
from omnigent.superchat.decks.kit import theme_source
from omnigent.superchat.decks.outline import (
    MAX_ELEMENT_CHARS,
    MAX_RANGE_CHARS,
    clip_source,
    deck_outline,
    element_source,
    slide_sources,
)
from omnigent.superchat.feature import HandlerCtx
from omnigent.superchat.sheets.table import TABLE_KINDS, read_table


def workspace_roots() -> list[Path]:
    """Directories a save may read from: the runner workspace, ``~/workspace`` and the cwd."""
    candidates = [os.environ.get("OMNIGENT_RUNNER_WORKSPACE"), str(Path.home() / "workspace")]
    roots = [Path(c) for c in candidates if c]
    roots.append(Path.cwd())
    resolved: list[Path] = []
    for root in roots:
        try:
            real = root.resolve()
        except OSError:
            continue
        if real.is_dir() and real not in resolved:
            resolved.append(real)
    return resolved


def resolve_workspace_file(raw: str, roots: list[Path]) -> Path | str:
    """The real file for ``raw`` when it lies under a workspace root, else an error string."""
    given = Path(raw).expanduser()
    options = [given] if given.is_absolute() else [root / given for root in roots]
    for option in options:
        try:
            real = option.resolve()
        except OSError:
            continue
        if any(real.is_relative_to(root) for root in roots):
            if real.is_file():
                return real
            continue
    if any(o.exists() for o in options):
        return "path must be a file inside your workspace"
    return f"file not found: {raw}"


def _read(path: Path) -> bytes | str:
    size = path.stat().st_size
    if size == 0:
        return "file is empty"
    if size > MAX_ARTIFACT_BYTES:
        return f"file too large ({size // (1024 * 1024)} MB; 25 MB max)"
    return path.read_bytes()


async def handle_artifact_tool(ctx: HandlerCtx, args: dict[str, Any]) -> str:
    """
    Run one artifact tool.

    :param ctx: The call context.
    :param args: Parsed tool arguments.
    :returns: Tool output JSON string.
    """
    tool_name = ctx.tool_name
    caller = await resolve_caller(ctx)
    if isinstance(caller, str):
        return caller
    chat_id = caller.chat_id
    if not chat_id:
        return error(f"{tool_name}: no chat to attach to")
    try:
        if tool_name == "artifact_list":
            resp = await caller.client.get(
                f"/v1/artifacts?parent_session_id={chat_id}", timeout=30.0
            )
        elif tool_name == "artifact_delete":
            artifact_id = args.get("artifact_id")
            if not isinstance(artifact_id, str) or not HEX_ID_RE.match(artifact_id):
                return error("artifact_delete requires a valid artifact_id")
            resp = await caller.client.delete(f"/v1/artifacts/{artifact_id}", timeout=30.0)
        elif tool_name == "artifact_patch":
            return await _patch(caller.client, caller.conversation_id, args, ctx)
        elif tool_name == "artifact_outline":
            return await _outline(caller.client, args)
        else:
            saver = caller.conversation_id if caller.is_helper else None
            return await _save(caller.client, chat_id, args, saver, ctx)
    except Exception as exc:  # noqa: BLE001
        return error(f"{tool_name} failed: {exc}")
    return finish(resp)


#: The engine's code for a save whose ``base_version`` is no longer the newest.
STALE_CODE = "artifact_stale"
#: A save or patch whose bytes equal the newest version's: no version was made.
UNCHANGED_CODE = "artifact_unchanged"


def _error_code(resp: Any) -> str | None:
    try:
        error = resp.json().get("error")
    except (ValueError, AttributeError):
        return None
    return error.get("code") if isinstance(error, dict) else None


async def _stale(client: Any, chat_id: str, file: Path, resp: Any) -> str:
    """A stale save from a file's studio: put the newest version in the workspace, say so.

    The model re-reads the file (now the newest bytes) and redoes its change on top; nothing it
    wrote is saved over someone else's version.
    """
    from omnigent.superchat.artifacts.writeback import write_bytes_idempotent

    newest: dict[str, Any] | None = None
    try:
        listed = await client.get(f"/v1/artifacts?parent_session_id={chat_id}", timeout=30.0)
        newest = next(
            (a for a in listed.json().get("artifacts", []) if a.get("name") == file.name), None
        )
        if newest is not None:
            content = await client.get(f"/v1/artifacts/{newest['id']}/content", timeout=60.0)
            content.raise_for_status()
            await asyncio.to_thread(write_bytes_idempotent, file, content.content)
    except Exception:  # noqa: BLE001 - the refusal stands either way
        return json.dumps({"error": STALE_CODE, "details": resp.text[:500]})
    version = newest.get("version") if newest else None
    return json.dumps(
        {
            "error": STALE_CODE,
            "newest_version": version,
            "message": (
                f"{file.name} was saved as v{version} meanwhile, so yours was not saved. The "
                f"workspace file now holds v{version}: read it again, redo the change on top of "
                f"it and save with base_version {version}."
            ),
        }
    )


async def _checked(
    client: Any, saved: dict[str, Any], data: bytes, ctx: HandlerCtx, source: Path | None
) -> dict[str, Any]:
    """What a save says about how the file renders: a deck's ``layout`` (the ``deck_check``
    render of the saved bytes), a page's ``look`` (what changed since the version before, seen
    in the render), ``{}`` for any other file. No separate check is needed after a save."""
    name = str(saved.get("name") or "")
    if is_deck_name(name):
        from omnigent.superchat.decks.authoring import saved_layout

        return {"layout": await saved_layout(data, ctx)}
    if not is_page(name):
        return {}
    before: bytes | None = None
    version = saved.get("version")
    if isinstance(version, int) and version > 1:
        try:
            resp = await client.get(
                f"/v1/artifacts/{saved['id']}/content?version={version - 1}", timeout=60.0
            )
            resp.raise_for_status()
            before = resp.content
        except Exception:  # noqa: BLE001 - without it the whole page is looked at
            before = None
    return {"look": await saved_look(data, before, ctx, source)}


async def _save(
    client: Any, chat_id: str, args: dict[str, Any], saver: str | None, ctx: HandlerCtx
) -> str:
    raw = args.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return error("artifact_save requires a path")
    file = resolve_workspace_file(raw.strip(), workspace_roots())
    if isinstance(file, str):
        return error(file)
    if kind_for_name(file.name) is None:
        return error(f"file type not allowed; use one of: {', '.join(KIND_MIME)}")
    data = await asyncio.to_thread(_read, file)
    if isinstance(data, str):
        return error(data)
    title = args.get("title")
    url = (
        f"/v1/artifacts?parent_session_id={chat_id}&name={quote(file.name)}"
        f"&source_path={quote(str(file))}"
    )
    if isinstance(title, str) and title.strip():
        url += f"&title={quote(title.strip()[:256])}"
    if saver:  # a Helper saves into its Conversation; the version records the Helper's turn
        url += f"&chat_id={quote(saver)}"
    base = args.get("base_version")
    if isinstance(base, int) and not isinstance(base, bool) and base >= 1:
        url += f"&base_version={base}"
    resp = await client.post(
        url, content=data, headers={"Content-Type": "application/octet-stream"}, timeout=120.0
    )
    if resp.status_code == 409 and _error_code(resp) == STALE_CODE:
        return await _stale(client, chat_id, file, resp)
    if resp.status_code >= 400:
        return finish(resp)
    saved = resp.json()
    if saved.get("unchanged"):
        # Not an artifact result: no file card, and no "saved" the model could repeat.
        version = saved.get("version")
        return json.dumps(
            {
                "error": UNCHANGED_CODE,
                "unchanged": True,
                "version": version,
                "message": (
                    f"{file.name} is identical to v{version}; nothing was saved. Check that "
                    "your edit was written to the file, then save again."
                ),
            }
        )
    # source_path lets the file search follow a saved file (knowledge's on_result listener).
    checked = await _checked(client, saved, data, ctx, file)
    return json.dumps({"type": "artifact", **saved, "source_path": str(file), **checked})


async def _patch(client: Any, studio_id: str, args: dict[str, Any], ctx: HandlerCtx) -> str:
    """``artifact_patch`` from a file's studio: patch the newest version, mirror it on disk.

    The saved bytes are written to the file's workspace path, so a later read or
    ``artifact_save`` of the file starts from them, and a deck's or a page's are checked
    (``layout``, ``look``). A stale base says which version is newest.
    """
    from omnigent.superchat.artifacts.writeback import write_bytes_idempotent

    artifact_id = args.get("artifact_id")
    base = args.get("base_version")
    patches = args.get("patches")
    if not isinstance(artifact_id, str) or not HEX_ID_RE.match(artifact_id):
        return error("artifact_patch requires the file's artifact_id (its group id)")
    if not isinstance(base, int) or isinstance(base, bool) or base < 1:
        return error("artifact_patch requires base_version: the version you are changing")
    if not isinstance(patches, list) or not patches:
        return error("artifact_patch requires at least one patch")
    resp = await client.post(
        f"/v1/artifacts/{artifact_id}/patch",
        json={"chat_id": studio_id, "base_version": base, "patches": patches},
        timeout=60.0,
    )
    if resp.status_code == 409 and _error_code(resp) == STALE_CODE:
        newest: int | None = None
        try:
            detail = await client.get(f"/v1/artifacts/{artifact_id}", timeout=30.0)
            versions = detail.json().get("all_versions") or []
            newest = versions[0].get("version") if versions else None
        except Exception:  # noqa: BLE001 - the refusal stands either way
            newest = None
        return json.dumps(
            {
                "error": STALE_CODE,
                "newest_version": newest,
                "message": (
                    f"The file was saved as v{newest} meanwhile, so nothing was changed. Read it "
                    f"again and redo the change with base_version {newest}."
                ),
            }
        )
    if resp.status_code >= 400:
        return finish(resp)
    saved = resp.json()
    if saved.get("unchanged"):
        version = saved.get("version")
        return json.dumps(
            {
                "error": UNCHANGED_CODE,
                "unchanged": True,
                "version": version,
                "message": (
                    f"The patches left the file exactly as v{version}; nothing was saved. Check "
                    "the element ids and values against the file, then patch again."
                ),
            }
        )
    name = str(saved.get("name") or "")
    source = saved.get("source_path")
    file = (
        resolve_workspace_file(source, workspace_roots())
        if isinstance(source, str) and source and saved.get("summary")
        else None
    )
    data: bytes | None = None
    in_place: Path | None = None  # the workspace file, once it holds the saved bytes
    if isinstance(file, Path) or is_deck_name(name) or is_page(name):
        try:
            content = await client.get(f"/v1/artifacts/{saved['id']}/content", timeout=60.0)
            content.raise_for_status()
            data = content.content
            if isinstance(file, Path):
                await asyncio.to_thread(write_bytes_idempotent, file, data)
                in_place = file
        except Exception:  # noqa: BLE001 - the version is saved; the next read refreshes
            pass
    if data is not None:
        checked = await _checked(client, saved, data, ctx, in_place)
    elif is_deck_name(name):
        checked = {"layout": {"checked": False, "why": "The saved deck could not be read back"}}
    elif is_page(name):
        checked = {"look": {"checked": False, "why": "The saved page could not be read back"}}
    else:
        checked = {}
    return json.dumps({"type": "artifact", **saved, **checked})


#: Lines of a text file the outline shows, and the characters they may take.
OUTLINE_LINES = 60
OUTLINE_MAX_CHARS = 4000
_TEXT_KINDS = frozenset({"html", "md", "txt", "json"})
#: Characters of one sheet header cell shown.
_HEADER_CELL = 40


def _sheet_headers(data: bytes, kind: str) -> str:
    lines: list[str] = []
    for sheet in read_table(data, kind):
        rows = sheet.get("rows") or []
        cells = [
            str(c.get("v") if c.get("v") is not None else "") for c in (rows[0] if rows else [])
        ]
        header = " | ".join(c.strip()[:_HEADER_CELL] for c in cells)
        size = f"{sheet.get('n_rows', 0)} rows, {sheet.get('n_cols', 0)} columns"
        lines.append(f"{sheet.get('name')} ({size}): {header or '(empty)'}")
    return "Sheets and their headers:\n" + "\n".join(lines)[:OUTLINE_MAX_CHARS]


def outline_text(name: str, kind: str, data: bytes) -> str:
    """A saved file's outline (ADR 0027): a deck's slides, a sheet's headers, or its first lines.

    :param name: The file name (a ``*.deck.html`` is a deck).
    :param kind: The artifact kind.
    :param data: The newest version's bytes.
    :returns: The outline text, bounded.
    """
    if kind == "html" and is_deck_name(name):
        outline = deck_outline(data)
        if outline:
            return outline
    if kind in TABLE_KINDS:
        try:
            return _sheet_headers(data, kind)
        except Exception:  # noqa: BLE001 - an unreadable sheet still gets an answer
            return f"{name} could not be read as a sheet."
    if kind in _TEXT_KINDS:
        lines = data.decode("utf-8", "replace").splitlines()
        head = "\n".join(lines[:OUTLINE_LINES])[:OUTLINE_MAX_CHARS]
        more = len(lines) - OUTLINE_LINES
        tail = f"\n… ({more} more lines)" if more > 0 else ""
        return f"First lines of {name}:\n{head}{tail}"
    return f"{name} is a {kind} file; it has no text outline."


#: The ``element`` that reads a deck's theme tokens and its own CSS instead of an element.
THEME_ELEMENT = "theme"


def _range(raw: object) -> tuple[int, int] | None:
    """``"3"`` or ``"2-4"`` as 1-based first and last, or ``None`` when it is not one."""
    first, _, last = str(raw).strip().partition("-")
    try:
        a, b = int(first), int(last or first)
    except ValueError:
        return None
    return (a, b) if 1 <= a <= b else None


def read_part(
    name: str, kind: str, data: bytes, element: object, span: object
) -> str | dict[str, Any]:
    """One element's source, or a run of slides (a deck) or lines (a text file); an error
    sentence when there is no such part. Never the whole file.
    """
    if element == THEME_ELEMENT and kind == "html" and is_deck_name(name):
        theme = theme_source(data.decode("utf-8", "replace"))
        if theme is None:
            return f"{name} has no theme markers."
        return {"element": element, "source": clip_source(theme, MAX_ELEMENT_CHARS)}
    if element:
        found = element_source(data, str(element)) if kind == "html" else None
        if found is None:
            return f"{name} has no element with data-superchat-id {element!r}; see its outline."
        return {"element": element, "source": found}
    bounds = _range(span)
    if bounds is None:
        return "range must be one number or two joined by a hyphen, e.g. 2 or 2-4"
    first, last = bounds
    if kind == "html" and is_deck_name(name):
        slides = slide_sources(data)
        if first > len(slides):
            return f"{name} has {len(slides)} slides."
        picked = "\n".join(slides[first - 1 : last])
        shown = f"{first}-{min(last, len(slides))}"
        return {"slides": shown, "source": clip_source(picked, MAX_RANGE_CHARS)}
    if kind not in _TEXT_KINDS:
        return f"{name} is a {kind} file; it has no lines to read."
    lines = data.decode("utf-8", "replace").splitlines()
    picked = "\n".join(lines[first - 1 : last])
    shown = f"{first}-{min(last, len(lines))}"
    return {"lines": shown, "source": clip_source(picked, MAX_RANGE_CHARS)}


async def _outline(client: Any, args: dict[str, Any]) -> str:
    """``artifact_outline``: the newest version's outline, or one element or range of it."""
    artifact_id = args.get("artifact_id")
    if not isinstance(artifact_id, str) or not HEX_ID_RE.match(artifact_id):
        return error("artifact_outline requires a valid artifact_id")
    meta = await client.get(f"/v1/artifacts/{artifact_id}", timeout=30.0)
    if meta.status_code >= 400:
        return finish(meta)
    item = meta.json()
    versions = [v.get("version") for v in item.get("all_versions") or []]
    newest = max((v for v in versions if isinstance(v, int)), default=item.get("version"))
    url = f"/v1/artifacts/{artifact_id}/content"
    if isinstance(newest, int):
        url += f"?version={newest}"
    content = await client.get(url, timeout=60.0)
    if content.status_code >= 400:
        return finish(content)
    name = str(item.get("name") or "")
    kind = str(item.get("kind") or kind_for_name(name) or "")
    head = {"artifact_id": item.get("group_id") or artifact_id, "name": name, "version": newest}
    if args.get("element") or args.get("range"):
        part = await asyncio.to_thread(
            read_part, name, kind, content.content, args.get("element"), args.get("range")
        )
        return error(part) if isinstance(part, str) else json.dumps({**head, **part})
    outline = await asyncio.to_thread(outline_text, name, kind, content.content)
    return json.dumps({**head, "outline": outline})
