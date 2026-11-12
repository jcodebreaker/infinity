"""
Shared helpers for materializing multimodal attachment blocks to disk.

Both native executors (Claude Code, Codex) receive user messages whose
image/file content blocks carry resolved base64 data URIs. Inlining that
base64 into the text sent to the native CLI is wrong: Claude Code cannot
view it, and the Codex app-server rejects any turn whose input text
exceeds 1 MiB (``input_too_large``). Instead each executor decodes the
data URI to a file on disk and references it by path — Claude Code via
its Read tool, Codex via a ``localImage`` input item. This module owns
that shared decode-and-write step.
"""

from __future__ import annotations

import base64
import logging
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

import httpx

_logger = logging.getLogger(__name__)

# Characters that would corrupt a "[Attached: ...]" / "[Attachment ...]"
# marker line for the consumers that regex-match it (forwarders, title
# seeding): brackets end the match early, newlines break the line shape.
_MARKER_UNSAFE = re.compile(r"[\[\]\r\n]")
FRAMEWORK_NOTICE_BLOCK_TYPE = "_omnigent_framework_notice"

# Maps a data-URI MIME type to the file extension used when no filename
# is supplied, e.g. ``"image/png"`` -> ``".png"``.
MIME_TO_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
}


@dataclass(frozen=True)
class DataUri:
    """
    Decoded components of a ``data:`` URI.

    :param mime_type: The MIME type, e.g. ``"image/png"``.
    :param base64_payload: The base64-encoded payload following the
        comma, e.g. ``"iVBORw0KGgo..."``.
    """

    mime_type: str
    base64_payload: str


def parse_data_uri(uri: str) -> DataUri:
    """
    Split a ``data:`` URI into its MIME type and base64 payload.

    :param uri: Data URI string,
        e.g. ``"data:image/png;base64,iVBOR..."``.
    :returns: A :class:`DataUri` with the MIME type and base64 payload.
    :raises ValueError: If the URI has no comma separating header from
        payload.
    """
    # "data:image/png;base64,iVBOR..."
    header, _, payload = uri.partition(",")
    if not payload:
        raise ValueError(f"Malformed data URI: no comma separator in {uri[:80]}")
    # header = "data:image/png;base64"
    mime_part = header.removeprefix("data:").removesuffix(";base64")
    return DataUri(mime_type=mime_part, base64_payload=payload)


# Upload limits for files that the harness opens with filesystem tools.
# The server enforces these per session, including copies between sessions.
MAX_FILESYSTEM_ATTACHMENT_UPLOAD_BYTES: int = 50 * 1024 * 1024
MAX_SESSION_FILESYSTEM_ATTACHMENTS: int = 20
MAX_SESSION_FILESYSTEM_ATTACHMENT_BYTES: int = 200 * 1024 * 1024

# These formats require the harness's filesystem tools. Match by extension
# because browsers can mislabel Office documents as ZIP or generic binary data.
_FILESYSTEM_ATTACHMENT_EXTENSIONS: frozenset[str] = frozenset(
    {".zip", ".docx", ".xlsx", ".pptx", ".db", ".sqlite", ".sqlite3"}
)

# Advertised by builds that can deliver and restore these attachments.
CAP_FILESYSTEM_ATTACHMENTS = "filesystem_attachments"


def requires_filesystem(filename: str | None) -> bool:
    """
    Whether an attachment needs a harness that can open local files.

    :param filename: The original filename, e.g. ``"report.docx"``.
    :returns: True for supported archives, Office documents, and databases.
    """
    return bool(
        filename and PurePath(filename).suffix.lower() in _FILESYSTEM_ATTACHMENT_EXTENSIONS
    )


def inline_filesystem_attachment_name(content: object) -> str | None:
    """
    Filename of the first attachment requiring filesystem tools with inline bytes.

    These files must arrive as uploaded ``file_id`` references, so the
    upload route's harness, denylist, and quota checks run before any bytes
    reach the sandbox.

    :param content: A message's content blocks.
    :returns: The offending filename, e.g. ``"payload.zip"``, or ``None``.
    """
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict):
            continue
        filename = block.get("filename")
        if not isinstance(filename, str) or not requires_filesystem(filename):
            continue
        if block.get("file_data") or block.get("image_url"):
            return filename
    return None


# Regex source matching the exact line unresolved_attachment_marker() emits.
# Consumers (title synthesis, TUI forwarders) compose their marker-matching
# patterns from this so the shapes cannot drift apart.
UNRESOLVED_ATTACHMENT_MARKER_PATTERN = r"\[Attachment [^\]]+ could not be loaded\]"

# TUI forwarders strip local file paths from mirrored chat bubbles.
# Codex's binary file inputs also use the "[Attached file: ...]" shape.
ATTACHMENT_MARKER_STRIP_PATTERN = (
    rf"\[Attached(?: file)?:[^\]]*\]|{UNRESOLVED_ATTACHMENT_MARKER_PATTERN}"
)


def unresolved_attachment_marker(block: Mapping[str, object]) -> str:
    """
    Visible placeholder for an attachment that could not be loaded.

    Callers emit this in place of the usual path reference when
    :func:`materialize_attachment` fails, so the model (and the mirrored
    transcript) sees that an attachment was lost instead of silently
    receiving nothing and hallucinating its content.

    :param block: The content block that failed to materialize. Named by
        its ``filename``, falling back to ``file_id`` then ``"attachment"``,
        with marker-breaking characters replaced by ``_``.
    :returns: Marker line, e.g.
        ``"[Attachment photo.png could not be loaded]"``. Always matches
        :data:`UNRESOLVED_ATTACHMENT_MARKER_PATTERN`.
    """
    name = str(block.get("filename") or block.get("file_id") or "attachment")
    return f"[Attachment {_MARKER_UNSAFE.sub('_', name)} could not be loaded]"


def has_unresolved_file_id(block: Mapping[str, object]) -> bool:
    """
    True if *block* carries a ``file_id`` no resolver has inlined yet.

    :param block: Message content block dict.
    :returns: Whether the block still needs :func:`resolve_file_id_block`.
    """
    file_id = block.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return False
    data_uri = block.get("image_url") or block.get("file_data")
    return not (isinstance(data_uri, str) and data_uri.startswith("data:"))


def resize_notice(source_metadata: object) -> str | None:
    """
    Model-facing note that an uploaded image was downscaled, or ``None``.

    The single source of truth for the resize-notice wording, shared by
    every attachment-resolution path (the in-process resolver in
    ``omnigent.runtime.content_resolver`` and the runner/native-harness
    resolver in :func:`resolve_file_id_block`) so the two never drift.

    :param source_metadata: A stored file's ``source_metadata`` dict (see
        :class:`omnigent.entities.file.StoredFile`). A downscaled image
        carries the pre-downscale ``width`` / ``height``.
    :returns: The notice text when the image was downscaled, else ``None``
        (passthrough images, non-images, or absent metadata).
    """
    dimensions = resize_dimensions(source_metadata)
    if dimensions is None:
        return None
    width, height = dimensions["width"], dimensions["height"]
    return (
        f"Note: the attached image was downscaled from {width}×{height} px to fit "
        "size limits, so you are viewing a lower-resolution version. Ask the user "
        "for a crop of the original if you need finer detail — re-uploading the whole "
        "image would be downscaled the same way."
    )


def resize_dimensions(source_metadata: object) -> dict[str, int] | None:
    """Return positive integer image dimensions from stored metadata."""
    if not isinstance(source_metadata, Mapping):
        return None
    width, height = source_metadata.get("width"), source_metadata.get("height")
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        return None
    return {"width": width, "height": height}


def reject_authored_framework_notices(content: object) -> object:
    """Reject reserved context blocks in authored message content."""
    if isinstance(content, dict):
        if content.get("type") == FRAMEWORK_NOTICE_BLOCK_TYPE:
            raise ValueError("Framework notice blocks are reserved for attachment resolution")
        for value in content.values():
            reject_authored_framework_notices(value)
    elif isinstance(content, list):
        for value in content:
            reject_authored_framework_notices(value)
    return content


def framework_notice_block(source_metadata: Mapping[str, object]) -> dict[str, object]:
    """Build transient model context that must not become user text."""
    return {"type": FRAMEWORK_NOTICE_BLOCK_TYPE, "source_metadata": dict(source_metadata)}


def framework_notices(content: object) -> list[str]:
    """Extract transient framework notices from structured content."""
    if not isinstance(content, list):
        return []
    notices: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != FRAMEWORK_NOTICE_BLOCK_TYPE:
            continue
        text = resize_notice(block.get("source_metadata"))
        if text:
            notices.append(text)
    return notices


def expand_framework_notices(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Render structured notices as system context at a provider boundary."""
    result: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            result.append(message)
            continue
        visible_content = [
            block
            for block in content
            if not isinstance(block, dict) or block.get("type") != FRAMEWORK_NOTICE_BLOCK_TYPE
        ]
        if len(visible_content) == len(content):
            result.append(message)
            continue
        result.extend(
            {"role": "system", "content": [{"type": "input_text", "text": notice}]}
            for notice in framework_notices(content)
        )
        if visible_content or not content:
            result.append({**message, "content": visible_content})
    return result


async def resolve_file_id_block(
    block: Mapping[str, object],
    *,
    session_id: str,
    client: httpx.AsyncClient,
) -> tuple[dict[str, object], dict[str, int] | None] | None:
    """
    Fetch a ``file_id`` attachment's bytes and inline them as a data URI.

    Used wherever message content must be consumed away from the server's
    file store (the out-of-process runner, transcript rebuilds): the bytes
    are fetched back through the session-scoped file resource endpoints
    and inlined under ``image_url`` (images) or ``file_data`` (other
    files).

    :param block: Content block for which :func:`has_unresolved_file_id`
        is true.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param client: HTTP client pointed at the Omnigent server.
    :returns: ``(rebuilt_block, notice)`` — the block without ``file_id``,
        and source dimensions for a sibling framework block, or ``None``.
        Returns ``None`` (not a tuple) when the fetch failed, so callers
        keep the original block and a visible marker can surface downstream.
    """
    file_id = str(block.get("file_id"))
    base = (
        f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}"
        f"/resources/files/{urllib.parse.quote(file_id, safe='')}"
    )
    try:
        meta_resp = await client.get(base, timeout=10.0)
        content_resp = await client.get(f"{base}/content", timeout=30.0)
        meta_resp.raise_for_status()
        content_resp.raise_for_status()
    except httpx.HTTPError:
        _logger.warning(
            "failed to resolve file_id=%s for session=%s",
            file_id,
            session_id,
            exc_info=True,
        )
        return None

    try:
        parsed = meta_resp.json() if meta_resp.content else {}
    except ValueError:
        parsed = None
    meta = parsed if isinstance(parsed, dict) else {}
    if meta_resp.content and not meta:
        # Unusable metadata only costs the media-type hint; the content
        # response's Content-Type header still provides it.
        _logger.warning(
            "unusable file metadata for file_id=%s in session=%s; "
            "falling back to the content headers",
            file_id,
            session_id,
        )
    content_type = meta.get("content_type")
    if not isinstance(content_type, str) or not content_type:
        content_type = content_resp.headers.get("content-type") or "application/octet-stream"
    # Strip any charset suffix: data URIs need the media type hint.
    content_type = content_type.split(";", 1)[0]
    encoded = base64.b64encode(content_resp.content).decode("ascii")
    new_block = {k: v for k, v in block.items() if k != "file_id"}
    stored_name = meta.get("name")
    if isinstance(stored_name, str) and stored_name:
        # The stored name decides delivery. The block's own filename comes from
        # the client and could steer an upload past the upload checks.
        new_block["filename"] = stored_name
    notice: dict[str, int] | None = None
    if block.get("type") == "input_image":
        new_block["image_url"] = f"data:{content_type};base64,{encoded}"
        resource_metadata = meta.get("metadata")
        if isinstance(resource_metadata, Mapping):
            notice = resize_dimensions(resource_metadata.get("source_metadata"))
    else:
        new_block["file_data"] = f"data:{content_type};base64,{encoded}"
    return new_block, notice


async def resolve_session_item_file_references(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Inline ``file_id`` attachment blocks in rebuilt history as base64 data URIs.

    Message items come back from the server with the upload's raw ``file_id``.
    A cold-resume rebuild runs where no file/artifact stores exist, so bytes are
    fetched back through the session file endpoints, as for a live turn. A failed
    fetch is non-fatal: the block stays unresolved and surfaces a visible marker.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param items: Flat API item dicts from ``GET /v1/sessions/{id}/items``.
    :returns: The same items with resolvable attachment blocks rewritten
        to carry ``image_url`` / ``file_data`` data URIs.
    """
    for item in items:
        content = item.get("content")
        if item.get("type") != "message" or not isinstance(content, list):
            continue
        resolved_content: list[object] = []
        for block in content:
            if not (isinstance(block, dict) and has_unresolved_file_id(block)):
                resolved_content.append(block)
                continue
            result = await resolve_file_id_block(block, session_id=session_id, client=client)
            if result is None:
                resolved_content.append(block)
                continue
            new_block, notice = result
            resolved_content.append(new_block)
            if notice is not None:
                resolved_content.append(framework_notice_block(notice))
        item["content"] = resolved_content
    return items
