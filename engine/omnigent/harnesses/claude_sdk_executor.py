"""ClaudeSDKExecutor: run agents using the Claude Agent SDK.

Uses the ``claude-agent-sdk`` Python package to run Claude Code as the
underlying agent harness.  Omnigent tools are bridged into the SDK session
as MCP tools so Claude can call them alongside its built-in capabilities.

The SDK manages its own internal agent loop (tool calls, retries, context).
This executor translates the SDK message stream into Omnigent ExecutorEvents
and builds up the session History from observed tool-use blocks.

Requirements:
    pip install claude-agent-sdk          # optional dependency

Environment (direct Anthropic):
    ANTHROPIC_API_KEY – API key for Claude

Environment (gateway): the executor sets ANTHROPIC_BASE_URL plus an
invocation-local apiKeyHelper setting from the gateway base URL and auth
command, so Claude Code can refresh auth during long sessions.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import pathlib
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, NamedTuple, Protocol, TypeAlias, cast
from urllib.parse import urlparse

from omnigent._platform import resolve_cli_binary, stable_user_id
from omnigent.context.labels import SUPERSIDE_CHAT_MODE_VALUE
from omnigent.harnesses import _proc
from omnigent.harnesses.agent_env import prompt_cache_1h
from omnigent.harnesses.bundle_skills import ensure_bundle_plugin_manifest
from omnigent.harnesses.hook_scripts import subagent_router
from omnigent.llms._usage_observer import notify_from_dict as _notify_usage_from_dict
from omnigent.llms.adapters._content import parse_data_uri as _parse_replay_data_uri
from omnigent.models import model_catalog
from omnigent.models.claude_model_vocabulary import (
    ALIAS_MODEL_ENV_VARS,
    served_alias_pins,
    served_canonical_overrides,
)
from omnigent.models.model_metadata import concrete_reported_model
from omnigent.runtime.mcp_tool_result import decode_mcp_image_result
from omnigent.runtime.public_error_codes import (
    AUTH_FAILED,
    PROVIDER_UNAVAILABLE,
    classify_provider_failure,
)
from omnigent.spec.types import RetryPolicy
from omnigent.util.json_types import JsonObject as _JsonObject
from omnigent.util.reasoning_effort import CLAUDE_EFFORTS, validate_effort

from ._subprocess_lifecycle import close_anyio_subprocess_transport
from .async_utils import run_sync_on_thread
from .datamodel import OSEnvSandboxSpec, OSEnvSpec
from .executor import (
    ContextFill,
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ReasoningChunk,
    SteerOffer,
    SteerOutcome,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStarted,
    ToolCallStatus,
    ToolInputChunk,
    ToolSpec,
    TurnCancelled,
    TurnComplete,
    classify_tool_result,
    describe_exception,
    result_ends_turn,
)
from .native_attachments import framework_notices, unresolved_attachment_marker
from .sandbox import (
    create_exec_launcher,
    get_backend,
    resolve_sandbox,
    with_additional_read_roots,
    with_additional_write_files,
    with_additional_write_roots,
)

logger = logging.getLogger(__name__)

# Default auth-token refresh cadence (ms) for the vendor-neutral gateway
# transport when ``HARNESS_CLAUDE_SDK_GATEWAY_AUTH_REFRESH_INTERVAL_MS`` is
# unset.
_GATEWAY_AUTH_REFRESH_MS = 900_000
#: Emitted between consecutive assistant text blocks so they never glue together.
_TEXT_BLOCK_SEPARATOR = "\n\n"
_CLAUDE_CODE_ENABLE_TOOL_SEARCH_ENV = "ENABLE_TOOL_SEARCH"

# ---------------------------------------------------------------------------
# TypeAliases for Omnigent JSON-shaped boundary values. The SDK exchanges
# heterogeneous dicts at the transport and tool boundaries — named aliases
# here keep the executor ``object``-free while isolating the justified
# ``explicit-any`` boundary to a single place.
# ---------------------------------------------------------------------------

# Parsed tool arguments / tool result dict — JSON-shaped bags exchanged
# with the Omnigent tool executor and the SDK's MCP bridge.
ToolArgs: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]
ToolResult: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# MCP response payload (``content`` + optional ``isError``) returned to the
# Claude SDK from each MCP tool handler.
McpResponse: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# Tool executor callable wired in by ``omnigent.Session``.
ToolExecutor: TypeAlias = Callable[[str, ToolArgs], Awaitable[ToolResult]]

# Elicitation handler wired in by :class:`ExecutorAdapter`. Kept SDK-agnostic
# so the adapter does not import ``claude_agent_sdk`` types.
ElicitationHandler: TypeAlias = Callable[
    [str, ToolArgs],
    Awaitable[bool],
]

# Opaque SDK artifacts whose concrete shape we don't touch directly:
# - ``SdkMcpTool``: returned by ``sdk.tool(...)`` decorator and passed back
#   to ``sdk.create_sdk_mcp_server(tools=...)`` without field access.
# - ``ClaudeAgentOptions``: the SDK's dataclass; fields set via attribute
#   assignment after construction rather than typed kwargs.
SdkMcpTool: TypeAlias = Any  # type: ignore[explicit-any]
SdkOptions: TypeAlias = Any  # type: ignore[explicit-any]


# ---------------------------------------------------------------------------
# SDK-private reach Protocols.
#
# Some SDK surfaces are dynamic and the lifecycle reaches below its public
# types. Protocols keep the handful of public and private attributes this
# executor touches explicit.
#
# The private reaches (``_query``, ``_transport``, ``_process``,
# ``_stderr_task`` / ``_stderr_task_group``, etc.) are necessary to tear
# down the CLI subprocess tree when the SDK's own ``disconnect()`` path is unsafe
# (different event loop / task) or hangs. The SDK does not expose a
# supported equivalent, so we treat the private attributes as part of
# our integration contract and document them here.
# ---------------------------------------------------------------------------


class _Process(Protocol):
    """Subset of ``anyio.abc.Process`` / ``asyncio.subprocess.Process``.

    These fields are standard on both process abstractions — the SDK's
    transport uses an anyio process but the shape matches the asyncio one
    for the attributes we touch.
    """

    pid: int | None
    returncode: int | None

    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    async def wait(self) -> int: ...


class _CancelScope(Protocol):
    def cancel(self) -> None: ...


class _TaskGroup(Protocol):
    cancel_scope: _CancelScope


class _TaskHandle(Protocol):
    """Private view of the SDK's detached stderr-reader task.

    Current ``claude-agent-sdk`` (>=0.2.x) runs the stderr reader as a
    single task exposed as ``_stderr_task`` with a ``cancel()`` method;
    older revs used an anyio task group (``_stderr_task_group``). The
    executor probes both shapes during force-close, so both are typed
    optional on ``_ClaudeTransport`` below.
    """

    def cancel(self) -> None: ...


class _ClaudeQuery(Protocol):
    """Private view of ``claude_agent_sdk._internal.query.Query``.

    ``_closed`` is the SDK's "stop accepting messages" flag. ``_tg`` was a
    per-query task group in older SDK revs — absent in current revs but
    still probed so this executor handles both shapes.
    """

    _closed: bool
    _tg: _TaskGroup | None


class _Stream(Protocol):
    """Structural view of an anyio text stream. Only ``aclose`` is actually
    available on the real ``TextReceiveStream`` / ``TextSendStream``; the
    ``close`` / ``transport`` attributes probed during teardown are
    historical belt-and-suspenders cleanup and no-op on the current SDK.
    """

    async def aclose(self) -> None: ...


class _ClaudeTransport(Protocol):
    """Private view of ``SubprocessCLITransport`` internals we tear down.

    Kept minimal — only the attributes ``_force_close_client`` touches.
    """

    _process: _Process | None
    _stdout_stream: _Stream | None
    _stdin_stream: _Stream | None
    _stderr_stream: _Stream | None
    _stderr_task: _TaskHandle | None
    _stderr_task_group: _TaskGroup | None
    _ready: bool


class _ClaudeClient(Protocol):
    """Structural view of ``claude_agent_sdk.ClaudeSDKClient``.

    Covers the public methods the executor calls plus the two private
    attributes it clears during a force-close. Test doubles (see
    ``tests/test_claude_sdk_executor.py``) satisfy this Protocol
    structurally via ``SimpleNamespace`` / custom classes.
    """

    _query: _ClaudeQuery | None
    _transport: _ClaudeTransport | None

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def query(
        self,
        prompt: str | AsyncIterator[_JsonObject],
        session_id: str = "",
    ) -> None: ...
    async def set_model(self, model: str | None) -> None: ...
    async def interrupt(self) -> None: ...

    # receive_response yields heterogeneous SDK message objects — kept
    # as ``Any`` so the caller can ``isinstance``-narrow onto the SDK's
    # real types via ``_ClaudeSDK`` below without needing a union here.
    def receive_response(self) -> AsyncIterator[Any]: ...  # type: ignore[explicit-any]


class _StreamEventObj(Protocol):
    """Structural view of ``claude_agent_sdk.StreamEvent``."""

    event: dict[str, Any]  # type: ignore[explicit-any]  # SDK declares this as dict[str, Any]


class _AssistantMessageObj(Protocol):
    """Structural view of ``claude_agent_sdk.AssistantMessage``."""

    # Each content block is one of the SDK block classes; isinstance-narrowed
    # at the read sites below.
    content: list[Any]  # type: ignore[explicit-any]
    # The model the SDK actually used for this message, e.g.
    # ``"claude-opus-4-8"``. The only place the executor learns the concrete
    # model when the spec pins none and the gateway resolves it internally.
    model: str | None
    # The CLI's own failure marker on a synthetic provider-failure message
    # (``billing_error``, ``rate_limit``, ``authentication_failed`` ...), else ``None``.
    error: str | None


class _UserMessageObj(Protocol):
    """Structural view of ``claude_agent_sdk.UserMessage``."""

    content: str | list[Any]  # type: ignore[explicit-any]


class _ResultMessageObj(Protocol):
    """Structural view of ``claude_agent_sdk.ResultMessage``."""

    result: str | None
    is_error: bool | None
    usage: dict[str, Any] | None  # type: ignore[explicit-any]
    # HTTP status of the failing API call (CLI >= 2.1.110), else ``None``.
    api_error_status: int | None


class _SystemMessageObj(Protocol):
    """Structural view of ``claude_agent_sdk.SystemMessage``."""

    subtype: str
    data: dict[str, Any]  # type: ignore[explicit-any]


class _TextBlockObj(Protocol):
    text: str


class _ThinkingBlockObj(Protocol):
    thinking: str


class _ToolUseBlockObj(Protocol):
    id: str
    name: str
    input: ToolArgs


class _ToolResultBlockObj(Protocol):
    tool_use_id: str
    content: str | list[dict[str, Any]] | None  # type: ignore[explicit-any]
    is_error: bool | None


class _ClaudeSDK(Protocol):
    """Structural view of the ``claude_agent_sdk`` module.

    Tests swap in a fake with matching attributes, so we mirror what the
    executor actually pulls off the module. The ``*Message`` / ``*Block``
    attributes are declared as ``type`` so they can be used both as
    ``isinstance`` second args and as Protocol-implementing factories.
    """

    # Factories / callables the executor invokes. ``Callable[..., X]``
    # expands to an implicit ``Any`` arg spec under
    # ``disallow_any_explicit`` — the SDK's construction kwargs are opaque
    # at our boundary so that's the right abstraction level here.
    ClaudeSDKClient: Callable[..., _ClaudeClient]  # type: ignore[explicit-any]
    ClaudeAgentOptions: Callable[..., Any]  # type: ignore[explicit-any]
    tool: Callable[..., Any]  # type: ignore[explicit-any]
    create_sdk_mcp_server: Callable[..., Any]  # type: ignore[explicit-any]

    # Classes used as isinstance second args. Declared as ``type`` so the
    # checker accepts them in isinstance() while the real attributes are
    # the SDK's concrete classes. Test doubles assign plain ``type``
    # objects which satisfy this shape.
    AssistantMessage: type
    UserMessage: type
    SystemMessage: type
    ResultMessage: type
    TextBlock: type
    ThinkingBlock: type
    ToolUseBlock: type
    ToolResultBlock: type


_CONNECT_TIMEOUT_SECONDS = 60.0
_QUERY_START_TIMEOUT_SECONDS = 30.0
# When the response stream is quiet for this long we emit a warning,
# but keep waiting — a long-running native tool can legitimately block
# the stream far longer than any fixed deadline.
_STREAM_IDLE_WARN_SECONDS = 600.0

# ── Multimodal content block conversion ──────────────────────


def _get_inline_data_uri_info(value: Any) -> tuple[str, int] | None:  # type: ignore[explicit-any]
    """If ``value`` contains an inline ``data:*;base64,...`` URI, return its
    ``(media_type, base64_char_count)``; otherwise ``None``.

    A resolved attachment block looks like::

        {"type": "input_image",
         "filename": "screenshot.png",
         "image_url": "data:image/png;base64,iVBORw0KGgoAAAANS...=="}   # ~520k chars

    (non-image files carry the same payload under ``file_data`` instead of
    ``image_url``.) Returns e.g. ``("image/png", 519324)`` — the media type and
    payload size used to build the compact
    ``[image: screenshot.png, image/png, 519324 base64 chars]`` placeholder,
    so the raw base64 never lands in the ``Conversation so far:`` prompt text.
    """
    if isinstance(value, str):
        parsed = _parse_replay_data_uri(value)
        if parsed is not None:
            return parsed.media_type, len(parsed.data)
        return None
    if isinstance(value, list):
        for item in value:
            found = _get_inline_data_uri_info(item)
            if found is not None:
                return found
        return None
    if isinstance(value, dict):
        for item in value.values():
            found = _get_inline_data_uri_info(item)
            if found is not None:
                return found
    return None


def _redact_inline_base64(value: Any) -> Any:  # type: ignore[explicit-any]
    """Deep-replace inline base64 attachment payloads with a compact
    ``[attachment: …]`` marker, recursing through dict/list values. The fallback
    path for values that reach ``json.dumps`` (nested dicts, non-block content)
    so a resolver-produced base64 payload does not survive serialization.

    Two shapes are redacted: a whole-string ``data:*;base64,...`` URI (the
    resolver form under ``image_url`` / ``file_data``), and an Anthropic content
    block ``{"type": "image"|"document", "source": {"type": "base64", ...}}``
    (what the ``Read`` tool returns for an image file, carried in a
    ``function_call_output``). Neither is redacted when it appears as a dict
    key, tuple member, or substring embedded mid-text (the runner never emits
    those)."""
    if isinstance(value, str):
        parsed = _parse_replay_data_uri(value)
        if parsed is None:
            return value
        return f"[attachment: {parsed.media_type}, {len(parsed.data)} base64 chars]"
    if isinstance(value, list):
        return [_redact_inline_base64(item) for item in value]
    if isinstance(value, dict):
        source = value.get("source")
        if (
            value.get("type") in ("image", "document")
            and isinstance(source, dict)
            and source.get("type") == "base64"
        ):
            media_type = source.get("media_type") or "application/octet-stream"
            data = source.get("data")
            payload_chars = len(data) if isinstance(data, str) else 0
            kind = "image" if value.get("type") == "image" else "attachment"
            return {
                "type": "text",
                "text": f"[{kind}: {media_type}, {payload_chars} base64 chars]",
            }
        return {key: _redact_inline_base64(item) for key, item in value.items()}
    return value


def _text_block(text: str) -> _JsonObject:
    """Build an Anthropic text content block holding *text*."""
    return {"type": "text", "text": text}


def _coalesce_text_blocks(blocks: list[_JsonObject]) -> list[_JsonObject]:
    """
    Merge runs of adjacent text blocks into one newline-joined block.

    Keeps the replayed transcript readable as prose and makes the
    text-only history render byte-identical to a plain string prompt.

    :param blocks: Anthropic content blocks in transcript order.
    :returns: The same sequence with consecutive text blocks merged.
    """
    merged: list[_JsonObject] = []
    for block in blocks:
        if block.get("type") == "text" and merged and merged[-1].get("type") == "text":
            merged[-1] = _text_block(f"{merged[-1]['text']}\n{block['text']}")
        else:
            merged.append(block)
    return merged


def _structured_history_block(block: _JsonObject) -> _JsonObject | None:
    """
    Convert a resolved history attachment to a real Anthropic block.

    :param block: A prior-turn content block carrying an inline data URI.
    :returns: The ``image``/``document`` block, or ``None`` when the block
        is not a convertible attachment shape — the caller then falls back
        to the compact text marker rather than dropping the attachment.
    """
    if block.get("type") not in ("input_image", "input_file"):
        return None
    try:
        # Covers a malformed data URI and a bad base64 payload alike
        # (``binascii.Error`` subclasses ``ValueError``).
        converted = _to_anthropic_content_blocks([block])
    except ValueError:
        return None
    return converted[0] if converted else None


def _render_prior_content_blocks(content: object) -> list[_JsonObject]:
    """Render one prior message's content for the ``Conversation so far:``
    replay as Anthropic content blocks.

    Why this exists: ``_build_prompt`` only serializes prior history when
    ``resume_session=False`` — a *fresh* SDK client that must replay existing
    multimodal history (forked/shared sessions, sub-agents with
    ``pass_history=True``, or a client restarted mid-session). By that point a
    historical image/file block's ``file_id`` has already been resolved to a
    ``data:<media>;base64,...`` URI.

    Those resolved attachments are replayed as **structured** ``image`` /
    ``document`` blocks, interleaved in order with the surrounding text, so the
    cold-started client can actually inspect the image instead of reading a
    description of it. Structured blocks are also what keeps the prompt small:
    ``json.dumps()``-ing the block flattened the whole base64 payload into
    prompt TEXT, so the model tokenized the bytes as text rather than counting
    them as an image — seven ~390KB PNGs expand to ~2.5M tokens, and one real
    shared session hit a 3.5M-token prompt against a 1M limit.

    Anything that cannot become a structured block never leaks bytes into text.
    A resolver-produced inline attachment *value* — a whole-string
    ``data:*;base64,...`` under ``image_url`` / ``file_data``, including nested
    in dict/list values — is redacted, and an unconvertible attachment falls
    back to a compact ``[image/attachment: <id>, <media_type>, <N> base64
    chars]`` marker that preserves *that an attachment was present* without its
    bytes. An attachment the resolver never inlined renders as the visible
    "could not be loaded" marker. (This does not attempt to cover non-resolver
    shapes such as a data URI used as a dict key, a tuple member, or a substring
    embedded mid-text — the runner never emits those.)
    """
    if isinstance(content, str):
        sanitized = _redact_inline_base64(content)
        return [_text_block(str(sanitized))]
    if not isinstance(content, list):
        return [_text_block(json.dumps(_redact_inline_base64(content), ensure_ascii=True))]

    rendered: list[_JsonObject] = []
    for raw_block in content:
        block = cast(_JsonObject, raw_block) if isinstance(raw_block, dict) else None
        if block is not None:
            block_type = block.get("type")
            text = block.get("text")
            if block_type in ("input_text", "output_text", "text") and isinstance(text, str):
                rendered.append(_text_block(str(_redact_inline_base64(text))))
                continue

            inline_data = _get_inline_data_uri_info(block)
            if inline_data is not None:
                structured = _structured_history_block(block)
                if structured is not None:
                    rendered.append(structured)
                    continue
                media_type, payload_chars = inline_data
                kind = "image" if block_type == "input_image" else "attachment"
                identifier = block.get("filename") or block.get("file_id") or block_type
                rendered.append(
                    _text_block(
                        f"[{kind}: {identifier}, {media_type}, {payload_chars} base64 chars]"
                    )
                )
                continue

            if block.get("file_id"):
                # No inline bytes and no resolved payload: the resolver never
                # inlined this attachment. Serializing the raw block would read
                # as if the attachment were present; say it was lost instead.
                rendered.append(_text_block(unresolved_attachment_marker(block)))
                continue

        rendered.append(
            _text_block(json.dumps(_redact_inline_base64(raw_block), ensure_ascii=True))
        )
    return rendered


def _parse_data_uri(uri: str) -> tuple[str, str]:
    """
    Parse a ``data:`` URI into ``(media_type, base64_data)``.

    :param uri: A data URI, e.g.
        ``"data:image/png;base64,iVBOR..."``.
    :returns: Tuple of ``(media_type, base64_payload)``.
    :raises ValueError: If the URI is not a valid ``data:`` URI.
    """
    if not uri.startswith("data:"):
        raise ValueError(f"Not a data URI: {uri[:40]!r}")
    header, _, payload = uri[5:].partition(",")
    media_type = header.replace(";base64", "")
    return media_type, payload


def _to_anthropic_content_blocks(
    blocks: Sequence[object],
) -> list[_JsonObject]:
    """
    Convert Responses API content blocks to Anthropic Messages
    API content block format.

    Mapping:

    - ``input_text`` / ``output_text`` → ``{"type": "text", ...}``
    - ``input_image`` (with ``image_url`` data URI) →
      ``{"type": "image", "source": {"type": "base64", ...}}``
    - ``input_file`` (with ``file_data`` data URI) →
      ``{"type": "document", "source": {"type": "base64", ...}}``

    :param blocks: Responses API content block dicts.
    :returns: Anthropic API content block dicts.
    """
    result: list[_JsonObject] = []
    for raw_block in blocks:
        if not isinstance(raw_block, dict):
            raise ValueError("Anthropic content blocks must be objects")
        block = cast(_JsonObject, raw_block)
        block_type = block.get("type")
        if block_type in ("input_text", "output_text", "text"):
            result.append({"type": "text", "text": block["text"]})
        elif block_type == "input_image":
            image_url = block.get("image_url")
            if not isinstance(image_url, str) or not image_url:
                raise ValueError(
                    "input_image block is missing the 'image_url' field. "
                    "Upload the image via the session files API and reference "
                    "it by file_id so the content resolver can inline it."
                )
            if not image_url.startswith("data:"):
                raise ValueError(
                    "input_image block has a URL instead of a data URI. "
                    "Upload the image via the session files API and reference "
                    "it by file_id instead."
                )
            media_type, data = _parse_data_uri(image_url)
            result.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": data,
                    },
                }
            )
        elif block_type == "input_file":
            file_data = block.get("file_data")
            if not isinstance(file_data, str) or not file_data:
                raise ValueError(
                    "input_file block is missing the 'file_data' field. "
                    "Upload the file via the session files API and reference "
                    "it by file_id so the content resolver can inline it."
                )
            if not file_data.startswith("data:"):
                raise ValueError(
                    "input_file block has a URL instead of a data URI. "
                    "Upload the file via the session files API and reference "
                    "it by file_id instead."
                )
            media_type, data = _parse_data_uri(file_data)
            if media_type == "application/pdf":
                # Anthropic's base64 document source only accepts PDF.
                result.append(
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": data,
                        },
                    }
                )
            else:
                # All other text files (markdown, plain text, code, etc.)
                # must use Anthropic's "text" source type with decoded content.
                text_content = base64.b64decode(data).decode("utf-8", errors="replace")
                result.append(
                    {
                        "type": "document",
                        "source": {
                            "type": "text",
                            "media_type": "text/plain",
                            "data": text_content,
                        },
                    }
                )
    return result


async def _multimodal_message_iter(
    content_blocks: list[_JsonObject],
    *,
    session_id: str,
) -> AsyncIterator[_JsonObject]:
    """
    Yield a single structured user message dict for the Claude
    SDK's ``AsyncIterable[dict]`` query path.

    The SDK transport writes each yielded dict as a JSONL line to
    the CLI's stdin. The CLI forwards the content blocks to the
    Anthropic Messages API, which supports multimodal input.

    :param content_blocks: Anthropic API content block dicts
        (output of :func:`_to_anthropic_content_blocks`).
    :param session_id: The SDK session identifier.
    :yields: A single message dict.
    """
    yield {
        "type": "user",
        "message": {"role": "user", "content": content_blocks},
        "parent_tool_use_id": None,
        "session_id": session_id,
    }


async def _steer_message_iter(
    text: str,
    *,
    session_id: str,
    message_uuid: str,
) -> AsyncIterator[_JsonObject]:
    """Yield one user message for the CLI, to be taken up by the running turn.

    ``priority: "next"`` is the CLI's own mid-turn delivery (the next tool boundary, not an
    interrupt); ``uuid`` is echoed back by ``--replay-user-messages`` once it is taken up.
    """
    yield {
        "type": "user",
        "message": {"role": "user", "content": text},
        "parent_tool_use_id": None,
        "session_id": session_id,
        "uuid": message_uuid,
        "priority": "next",
    }


# Diagnostic knob: when set (any truthy value), skip wrapping the CLI
# via ``create_exec_launcher``. Used to isolate whether the silent
# connect hang is sandbox-related vs. inside the binary itself.
_NO_SANDBOX_ENV = "OMNIGENT_CLAUDE_SDK_NO_SANDBOX"

# Env override for an explicit claude binary, mirroring codex's
# OMNIGENT_CODEX_PATH. Set this when claude lives on a PATH the host
# daemon doesn't inherit (e.g. an nvm-managed global bin dir).
_CLAUDE_PATH_ENV = "OMNIGENT_CLAUDE_PATH"

# superside-chat turns off every Claude Code feature that would compete
# with Omnigent for ownership of context/sub-agents/memory (ADR
# docs/adr/0006-omnigent-owns-everything.md). Same env vars
# claude_native/main.py sets for a rollover terminal.
_CLAUDE_CODE_DISABLE_AUTO_COMPACT_ENV = "DISABLE_AUTO_COMPACT"
_CLAUDE_CODE_DISABLE_AUTO_MEMORY_ENV = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"
# The SDK's built-in sub-agent tool (``agents`` / "Agent tool" per
# ClaudeAgentOptions.agents); "Task" is its historical CLI name. Omnigent
# sub-agents are Omnigent sessions (see ``sys_session_create``), never this.
_SDK_BUILTIN_SUBAGENT_TOOLS = ("Agent", "Task")
# The CLI's native checklist tools. The model keeps its checklist with Omnigent's ``checklist``
# tool on every harness, so these stay disallowed: one list, one tool.
_CLI_TASK_TOOLS = ("TaskCreate", "TaskUpdate", "TaskList", "TaskGet")


def _usage_from_observed_call(
    last_call_usage: dict[str, Any] | None,  # type: ignore[explicit-any]
    model: str | None,
) -> dict[str, Any] | None:  # type: ignore[explicit-any]
    """Synthesize turn usage from the last observed ``message_start`` call.

    Used when a turn ends without ``ResultMessage`` usage (early stream
    close, terminal error, executor exception): the per-call prompt size
    from the most recent ``message_start`` is the best available window
    fill, so the context-occupancy meter can still advance.

    :param last_call_usage: The last ``message_start`` event's
        ``message.usage`` dict, or ``None`` when no API call started.
    :param model: Harness-reported model for cost pricing, e.g.
        ``"claude-sonnet-4-20250514"``.
    :returns: A usage dict shaped like ``TurnComplete.usage``
        (``output_tokens`` reports 0 — unknown on an incomplete turn),
        or ``None`` when nothing was observed.
    """
    if last_call_usage is None:
        return None
    ctx_in = last_call_usage.get("input_tokens") or 0
    ctx_cc = last_call_usage.get("cache_creation_input_tokens") or 0
    ctx_cr = last_call_usage.get("cache_read_input_tokens") or 0
    # ``total_tokens`` is billing-shaped (non-cache input only, no output on
    # an incomplete turn); ``context_tokens`` is window fill, so it includes
    # the cache buckets. The two intentionally differ.
    return {
        "input_tokens": ctx_in,
        "output_tokens": 0,
        "total_tokens": ctx_in,
        "context_tokens": ctx_in + ctx_cc + ctx_cr,
        "model": model,
    }


def _sum_usage(
    first: dict[str, Any],  # type: ignore[explicit-any]
    second: dict[str, Any],  # type: ignore[explicit-any]
) -> dict[str, Any]:  # type: ignore[explicit-any]
    """Add two ``ResultMessage.usage`` dicts: numbers sum (nested buckets too), others take
    the latest value, e.g. ``{"input_tokens": 3, "service_tier": "standard"}``."""
    total = dict(first)
    for key, value in second.items():
        prior = total.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float, dict)):
            total[key] = value
        elif isinstance(value, dict):
            total[key] = _sum_usage(prior if isinstance(prior, dict) else {}, value)
        elif isinstance(prior, (int, float)) and not isinstance(prior, bool):
            total[key] = prior + value
        else:
            total[key] = value
    return total


def _sandbox_disabled_by_env() -> bool:
    """``True`` when the diagnostic bypass env var is set to a truthy
    value. Emits a WARNING on activation so CI output unambiguously
    confirms the bypass was in effect for a given run.
    """
    if os.environ.get(_NO_SANDBOX_ENV):
        logger.warning(
            "Sandbox bypass active (%s is set); skipping create_exec_launcher.",
            _NO_SANDBOX_ENV,
        )
        return True
    return False


def _terminate_process_tree(process: _Process | None) -> None:
    _proc.terminate_tree(process)


def _kill_process_tree(process: _Process | None) -> None:
    _proc.kill_tree(process)


@contextmanager
def _unset_env_var(name: str) -> Iterator[None]:
    """
    Temporarily remove an env var from ``os.environ`` for the duration of
    the ``with`` block, then restore it (or leave it absent if it was not
    set before).

    Used around the claude-cli subprocess spawn to strip ``CLAUDECODE``
    when our own Python process is itself running under Claude Code — the
    child cli otherwise reports a "nested session" error. The SDK builds
    the child env as ``{**os.environ, **options.env, ...}``, so the only
    way to *remove* (not just override with ``""``) a key is to unset it
    in ``os.environ`` during the spawn.

    :param name: Env var name to remove for the block, e.g. ``"CLAUDECODE"``.
    :yields: Nothing; restores ``os.environ[name]`` on exit if it was set.
    """
    previous = os.environ.pop(name, None)
    try:
        yield
    finally:
        if previous is not None:
            os.environ[name] = previous


_CLOSE_ATTR: str = "close"
_TRANSPORT_ATTR: str = "transport"
_ACLOSE_ATTR: str = "aclose"


def _call_optional_method(obj: Any, name: str) -> None:  # type: ignore[explicit-any]
    """Call ``obj.<name>()`` if it exists and is callable, swallowing errors.

    Uses a runtime attribute name so this stays out of the
    ``getattr(..., "<literal>", ...)`` lint's crosshairs while still giving
    mypy a known shape (``Any`` at the boundary — the caller's concrete
    types don't declare the sync ``close`` hook we probe here).
    """
    method = getattr(obj, name, None)
    if callable(method):
        with suppress(Exception):
            method()


def _best_effort_close(resource: _Stream | _Process) -> None:
    """Invoke a best-effort synchronous close on an SDK-internal handle.

    The current SDK exposes ``aclose`` (async) on streams and a no-``close``
    anyio ``Process``; older revs and test doubles may still ship a sync
    ``close`` method. We probe for it via ``hasattr``-style helpers and
    swallow any failures — this runs only on the force-close teardown path
    where the alternative is leaking the handle.
    """
    _call_optional_method(resource, _CLOSE_ATTR)
    transport_obj = getattr(resource, _TRANSPORT_ATTR, None)
    if transport_obj is not None:
        _call_optional_method(transport_obj, _CLOSE_ATTR)


_CLAUDE_API_KEY_HELPER_ENV_KEY = "OMNIGENT_CLAUDE_API_KEY_HELPER"


@dataclass
class _ClaudeClientState:
    client: _ClaudeClient
    model: str | None
    loop: asyncio.AbstractEventLoop | None = None
    task: asyncio.Task[None] | None = None
    # Hash of the composed ``system_prompt`` this client was built with.
    # Only populated (and acted on) for superside-chat — see
    # ``_get_or_create_client``'s ``rebuild_on_instructions_change``.
    instructions_hash: str | None = None
    # False for a client launched ahead of the session's first turn (see
    # ``run_turn``'s pre-launch): it holds no history yet, so that turn replays it.
    primed: bool = True
    # What the client was launched with (:func:`_launch_key`), by part; a pre-launched
    # client that does not match its first turn is relaunched.
    launch_key: dict[str, str] | None = None


@dataclass
class _SteerWindow:
    """Mid-turn messages pushed into one live Claude SDK turn.

    ``open`` is true from the moment the turn's prompt is written until the turn's
    stream has finished with nothing outstanding; :meth:`ClaudeSDKExecutor.
    steer_session_message` only pushes while it is open, so a message can never be
    written to a CLI whose answer nobody reads (a one-turn-behind desync).
    ``pending`` maps the uuid of each pushed message the CLI has not echoed back yet to
    the caller's steer id.
    """

    open: bool = True
    pending: dict[str, str] = field(default_factory=dict)


def _call_metadata(call_id: str, message: object) -> dict[str, str]:
    """A tool event's metadata: its call id, and the SDK's ``parent_tool_use_id`` when the
    call was made inside another tool call (e.g. by a sub-agent)."""
    parent = getattr(message, "parent_tool_use_id", None)
    if isinstance(parent, str) and parent:
        return {"call_id": call_id, "parent_tool_use_id": parent}
    return {"call_id": call_id}


def _launch_key(
    system_prompt: str, tools: list[ToolSpec], effort: object, cfg: ExecutorConfig
) -> dict[str, str]:
    """What a Claude CLI launch is fixed to, a hash per part: prompt, tools, effort, mode."""
    parts: dict[str, object] = {
        "instructions": system_prompt,
        "tools": tools,
        "effort": effort,
        "mode": [cfg.extra.get("context_mode"), cfg.extra.get("max_turns")],
    }
    return {
        name: hashlib.sha256(
            json.dumps(value, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        for name, value in parts.items()
    }


#: Why a session's warm Claude CLI went away, by session key, kept across executors in this
#: harness process (the adapter replaces its executor after a turn that did not end cleanly),
#: so the launch that follows can say why it was needed.
_client_drop_reasons: dict[str, str] = {}


def _composed_instructions_hash(system_prompt: object) -> str | None:
    """Stable hash of a turn's composed ``system_prompt``, or ``None`` for none.

    Used only to detect when a superside-chat session's framework
    instructions (memory, rollover) changed since the warm client was
    built — never persisted, never compared across processes.

    :param system_prompt: ``ClaudeAgentOptions.system_prompt`` for this turn
        (a plain string in this executor; ``None`` when unset).
    :returns: A hex digest of *system_prompt*, or ``None`` when it is falsy.
    """
    if not system_prompt:
        return None
    import hashlib

    return hashlib.sha256(str(system_prompt).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PreparedClaudeCli:
    """Result of wrapping the Claude CLI in an Omnigent sandbox.

    :param cli_path: Path the SDK should exec for the Claude CLI.  May be the
        original system CLI or a generated wrapper script that applies the
        sandbox before exec-ing the real binary.  ``None`` when no CLI is
        available (no system ``claude`` on PATH).
    :param enable_native_tools: ``True`` when the sandbox is active and it is
        safe for the SDK to enable the built-in native OS tools (Bash, Read,
        Edit, …).  ``False`` when the sandbox cannot be applied (e.g. network
        denied, unsupported platform) — the caller should then leave native
        tools disabled.
    """

    cli_path: str | None
    enable_native_tools: bool


def _ensure_sdk() -> ModuleType:
    """Import and return the claude_agent_sdk module, raising a clear error if missing."""
    try:
        import claude_agent_sdk

        return claude_agent_sdk
    except ImportError as exc:
        raise ImportError(
            "ClaudeSDKExecutor requires the 'claude-agent-sdk' package. "
            "Install it with: pip install claude-agent-sdk"
        ) from exc


_OMNIGENT_MCP_PREFIX = "mcp__omnigent__"


def _args_key(args: object) -> str:
    """Tool arguments as one comparable string (the hook and the handler see equal dicts)."""
    return json.dumps(args, sort_keys=True, default=str)


def _build_mcp_tools(
    tool_schemas: list[ToolSpec],
    tool_executor: ToolExecutor | None,
    claim_call_id: Callable[[str, ToolArgs], str | None] | None = None,
) -> list[SdkMcpTool]:
    """Build SdkMcpTool objects from Omnigent tool schemas.

    Each tool is backed by a handler that calls the Omnigent tool_executor
    callback, which routes through the Session's tool registry (and thus
    respects policies, history recording, etc.). The SDK hands a handler only
    the arguments, so *claim_call_id* supplies the model's ``tool_use_id``:
    the dispatch then records the call under the same id the stream
    observation carries, whichever of the two reaches the adapter first.
    """
    sdk = cast(_ClaudeSDK, _ensure_sdk())

    mcp_tools: list[SdkMcpTool] = []
    for schema in tool_schemas:
        raw_name = schema.get("name")
        raw_desc = schema.get("description")
        # ``sdk.tool()`` requires ``str`` for name/description — the SDK
        # itself does not accept ``None``. Omnigent tool schemas always
        # carry a ``name`` (see ``Tool.tool_schema``); fall back to ``""``
        # only for the description, which is legitimately optional.
        tname: str = raw_name if isinstance(raw_name, str) else ""
        tdesc: str = raw_desc if isinstance(raw_desc, str) else ""
        tparams = schema.get("parameters", {"type": "object", "properties": {}})

        def _make_handler(tool_name: str) -> Callable[[ToolArgs], Awaitable[McpResponse]]:
            async def handler(args: ToolArgs) -> McpResponse:
                if tool_executor is None:
                    return {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {"error": f"No tool executor for '{tool_name}'"}
                                ),
                            }
                        ],
                    }
                try:
                    # ``tool_executor`` is declared as ``Awaitable[ToolResult]``
                    # so we always await. The isinstance guard below preserves
                    # the pre-refactor safety net for unexpected non-dict
                    # runtime returns without confusing the type checker.
                    call_id = claim_call_id(tool_name, args) if claim_call_id else None
                    raw = await (
                        tool_executor(tool_name, args, call_id=call_id)  # type: ignore[call-arg]
                        if call_id
                        else tool_executor(tool_name, args)
                    )
                    result: ToolResult = raw if isinstance(raw, dict) else {"result": raw}
                    image_result = decode_mcp_image_result(result)
                    if image_result is not None:
                        response: McpResponse = image_result.to_mcp()
                        # The SDK server reads snake_case; native MCP clients use isError.
                        response["is_error"] = image_result.is_error
                        return response
                    response = {
                        "content": [{"type": "text", "text": json.dumps(result)}],
                    }
                    if result.get("blocked") is True or (
                        "error" in result and result.get("error")
                    ):
                        response["isError"] = True
                    return response
                except Exception as exc:  # noqa: BLE001 — tool handler converts any error to MCP error response
                    return {
                        "content": [{"type": "text", "text": json.dumps({"error": str(exc)})}],
                        "isError": True,
                    }

            return handler

        decorated = sdk.tool(tname, tdesc, tparams)(_make_handler(tname))
        mcp_tools.append(decorated)
    return mcp_tools


def _augment_system_prompt_for_omnigent_mcp_tools(
    system_prompt: str,
    tool_schemas: list[ToolSpec],
) -> str:
    """
    Add Claude SDK-specific MCP tool-name guidance to the system prompt.

    Omnigent schemas use bare names such as ``sys_session_send``. The
    Claude SDK exposes tools from our in-process MCP server to the model
    as ``mcp__omnigent__<bare_name>``. Bundled agent prompts and skills use
    bare names because other executors call those directly, so the SDK needs
    a bridge note to stop the model from trying a non-existent bare tool first.
    """
    tool_names = [
        name for schema in tool_schemas if isinstance((name := schema.get("name")), str) and name
    ]
    if not tool_names:
        return system_prompt

    examples = [
        name
        for name in ("sys_session_rename", "sys_session_send", "sys_session_create")
        if name in tool_names
    ]
    if examples:
        example_text = "; ".join(
            f"use `mcp__omnigent__{name}` when instructions say `{name}`" for name in examples
        )
        note = (
            "Claude SDK tool naming: Omnigent tools are exposed as MCP tools. "
            f"{example_text}. For any other Omnigent tool, use "
            "`mcp__omnigent__<tool_name>` rather than the bare name."
        )
    else:
        note = (
            "Claude SDK tool naming: Omnigent tools are exposed as MCP tools. "
            "When instructions mention a bare Omnigent tool name, invoke "
            "`mcp__omnigent__<tool_name>` rather than the bare name."
        )

    if not system_prompt:
        return note
    return f"{system_prompt.rstrip()}\n\n{note}"


def _find_system_claude() -> str | None:
    """Find a system-installed ``claude`` CLI binary.

    Resolves via the ``OMNIGENT_CLAUDE_PATH`` override, then ``PATH``, then
    common global install dirs — so an nvm/npm-installed claude off the host
    daemon's frozen ``PATH`` is still found. Prefers the system install over
    the SDK's bundled CLI because the bundled version may be older and send
    beta flags a gateway may not support. Returns the absolute
    path, or ``None`` if not found.
    """
    return resolve_cli_binary("claude", env_var=_CLAUDE_PATH_ENV)


class _GatewayModelVocabulary(NamedTuple):
    """How a gateway's served ids map onto Claude Code's model vocabulary.

    :param alias_pins: ``{env_var: served_id}`` pinning each family alias.
    :param model_overrides: ``{canonical_id: served_id}`` rewrites for the ids
        Claude Code names itself.
    """

    alias_pins: dict[str, str]
    model_overrides: dict[str, str]


_EMPTY_GATEWAY_VOCABULARY = _GatewayModelVocabulary(alias_pins={}, model_overrides={})
#: Anthropic's own endpoint speaks the canonical ids; there is nothing to list or rewrite.
_DIRECT_ANTHROPIC_HOST = "api.anthropic.com"
#: Longest a launch waits on the gateway's model listing; a slower one finishes in the
#: background and serves the next launch.
_GATEWAY_LISTING_WAIT_SECONDS = 1.0
#: Gateway listings already read by this process, by (base URL, auth command).
_GATEWAY_VOCABULARIES: dict[tuple[str, str | None], _GatewayModelVocabulary] = {}


def _gateway_model_vocabulary(base_url: str, auth_command: str | None) -> _GatewayModelVocabulary:
    """Read Claude Code's model vocabulary off a gateway's model listing.

    Claude Code resolves a family alias — ``opus`` / ``sonnet`` / ``haiku`` /
    ``fable`` — through ``ANTHROPIC_DEFAULT_<FAMILY>_MODEL``, and every alias
    surface speaks them: the refusal-fallback (a flagged message re-issues on
    Opus), ``/model``, ``Agent``-tool spawns. Unpinned, an alias resolves to a
    canonical vendor id (``claude-opus-4-8``). A gateway that serves Claude under
    its own ids (``team-claude-opus-4-8``) rejects that with
    ``model_not_found``, and a refusal-fallback then kills the whole turn. List
    what the gateway serves and pin each alias to it.

    Claude Code reaches a model two ways, and a gateway with its own ids
    breaks both. Through a family alias — ``opus`` / ``sonnet`` / ``haiku`` /
    ``fable``, resolved via ``ANTHROPIC_DEFAULT_<FAMILY>_MODEL`` and spoken by
    ``/model`` and ``Agent``-tool spawns — which resolves to a canonical vendor
    id when unpinned. And by naming a canonical id itself: the refusal-fallback
    re-issues a safeguard-flagged turn on a model from a route table internal
    to the CLI. Either way the gateway answers ``model_not_found`` and the turn
    dies. One listing answers both: pin the aliases, and hand Claude Code the
    canonical-to-served rewrites for the ids it names on its own.

    :param base_url: The gateway's ``ANTHROPIC_BASE_URL``.
    :param auth_command: The gateway ``apiKeyHelper`` command; minted into the
        bearer the listing is fetched with.
    :returns: The pins and rewrites the listing supports. Empty — leaving
        today's behavior — when the gateway lists no Claude models or the
        listing cannot be fetched.
    """
    from omnigent.onboarding.provider_config import ANTHROPIC_FAMILY, GATEWAY_KIND

    provider = model_catalog.ResolvedModelProvider(
        kind=GATEWAY_KIND,
        family=ANTHROPIC_FAMILY,
        base_url=base_url,
        auth_command=auth_command,
        detail="claude-sdk gateway transport",
    )
    try:
        listing = model_catalog.listing_for_provider(provider)
    except Exception:
        logger.warning(
            "claude-sdk: could not list the gateway's models; leaving "
            "ANTHROPIC_DEFAULT_*_MODEL and modelOverrides unset",
            exc_info=True,
        )
        return _EMPTY_GATEWAY_VOCABULARY
    served = [entry.id for entry in listing.models if entry.family == "claude"]
    return _GatewayModelVocabulary(
        alias_pins={
            ALIAS_MODEL_ENV_VARS[alias]: model_id
            for alias, model_id in served_alias_pins(served).items()
        },
        model_overrides=served_canonical_overrides(served),
    )


async def _cached_gateway_vocabulary(
    base_url: str, auth_command: str | None
) -> _GatewayModelVocabulary:
    """The gateway's vocabulary, read once per process and waited on for at most a second.

    A listing slower than :data:`_GATEWAY_LISTING_WAIT_SECONDS` leaves this launch unpinned
    (today's behaviour when the listing fails) and is cached when it lands, for the next one.
    """
    key = (base_url, auth_command)
    cached = _GATEWAY_VOCABULARIES.get(key)
    if cached is not None:
        return cached

    async def _read() -> _GatewayModelVocabulary:
        vocabulary = await run_sync_on_thread(_gateway_model_vocabulary, base_url, auth_command)
        _GATEWAY_VOCABULARIES[key] = vocabulary
        return vocabulary

    listing = asyncio.ensure_future(_read())
    try:
        return await asyncio.wait_for(asyncio.shield(listing), _GATEWAY_LISTING_WAIT_SECONDS)
    except TimeoutError:
        logger.warning(
            "claude-sdk: the gateway's model listing took over %.0fs; launching without it",
            _GATEWAY_LISTING_WAIT_SECONDS,
        )
        return _EMPTY_GATEWAY_VOCABULARY


def _claude_settings_payload(
    api_key_helper: str | None, model_overrides: dict[str, str]
) -> str | None:
    """Serialize the invocation-local settings Claude Code launches with.

    :param api_key_helper: The gateway ``apiKeyHelper`` command, or ``None``.
    :param model_overrides: Canonical-to-served model id rewrites.
    :returns: Compact JSON for ``ClaudeAgentOptions.settings``, or ``None``
        when there is nothing to configure.
    """
    settings: dict[str, Any] = {}
    if api_key_helper:
        settings["apiKeyHelper"] = api_key_helper
    if model_overrides:
        settings["modelOverrides"] = model_overrides
    return json.dumps(settings, separators=(",", ":")) if settings else None


def _resolve_gateway_env(
    *,
    host_override: str | None = None,
    base_url_override: str | None = None,
    auth_command_override: str | None = None,
    auth_refresh_interval_ms: int | None = None,
) -> dict[str, str]:
    """Build Claude Code gateway env from the gateway transport values.

    The vendor-neutral gateway transport is a base URL + a bearer-token
    command + a refresh TTL, used verbatim.

    The bearer token itself is not returned. Claude Code receives an
    invocation-local ``apiKeyHelper`` setting and refresh TTL instead, so
    the CLI can periodically re-run the auth command during long sessions
    instead of inheriting a one-hour token snapshot.

    :param host_override: Gateway host origin, e.g.
        ``"https://gateway.example.com"``. When set, the gateway base URL
        and auth command values are required.
    :param base_url_override: ``ANTHROPIC_BASE_URL`` for the gateway.
        Populated from ``HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL``.
    :param auth_command_override: Shell command that prints a bearer token,
        e.g. ``"printf %s sk-..."``.
    :param auth_refresh_interval_ms: Refresh TTL in milliseconds, e.g.
        ``900000``.
    :returns: Environment values plus an internal apiKeyHelper command
        consumed by :meth:`ClaudeSDKExecutor.run_turn`, or ``{}`` when
        no gateway transport is supplied.
    :raises OSError: If a gateway host is present but missing the
        corresponding base URL or auth command.
    """
    if host_override:
        if base_url_override is None:
            raise OSError(
                "ClaudeSDKExecutor(gateway=True) with a gateway workspace host "
                "requires HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL."
            )
        if auth_command_override is None:
            raise OSError(
                "ClaudeSDKExecutor(gateway=True) with a gateway workspace host "
                "requires HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND."
            )
    if base_url_override is None or auth_command_override is None:
        return {}
    return {
        "ANTHROPIC_BASE_URL": base_url_override,
        "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": str(
            auth_refresh_interval_ms or _GATEWAY_AUTH_REFRESH_MS
        ),
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        _CLAUDE_API_KEY_HELPER_ENV_KEY: auth_command_override,
    }


def _parse_optional_int(value: str | None) -> int | None:
    """Parse an optional integer env-var value.

    :param value: Raw env-var value, e.g. ``"900000"``.
    :returns: Parsed integer, or ``None`` when unset or invalid.
    """
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        logger.warning("Ignoring invalid integer value %r", value)
        return None


def _claude_internal_write_roots() -> list[pathlib.Path]:
    """Writable roots the Claude CLI needs for its own local session state."""

    roots = [
        pathlib.Path.home() / ".claude" / "backups",
        pathlib.Path.home() / ".claude" / "plugins",
        pathlib.Path.home() / ".claude" / "session-env",
        pathlib.Path.home() / ".claude" / "sessions",
        pathlib.Path.home() / ".npm" / "_logs",
        pathlib.Path(tempfile.gettempdir()) / f"claude-{stable_user_id()}",
    ]
    for root in roots:
        root.mkdir(parents=True, exist_ok=True)
    return roots


def _claude_internal_write_files() -> list[pathlib.Path]:
    """Exact files the Claude CLI updates outside its writable roots."""

    # .credentials.json holds the Claude CLI's OAuth token on Linux.
    candidates = [
        pathlib.Path.home() / ".claude.json",
        pathlib.Path.home() / ".claude" / ".credentials.json",
    ]
    return [path for path in candidates if path.exists()]


def _resolve_sandbox_cwd(spec_cwd: str | None) -> pathlib.Path:
    """Resolve the sandbox root, rooting relative paths at the session
    working folder rather than the runner daemon's process cwd.

    A relative ``os_env.cwd`` — notably the default ``"."`` — resolved
    against ``os.getcwd()`` lands on the runner daemon's ``$HOME`` when
    no workspace is selected. That both roots the sandbox at the whole
    home dir and disagrees with the tmux terminal, which uses
    ``OMNIGENT_RUNNER_WORKSPACE``. Prefer that workspace as the base so
    the two agree; fall back to the process cwd only when it is unset.
    An absolute ``spec_cwd`` is honored verbatim.

    :param spec_cwd: The spec's ``os_env.cwd``, or ``None``.
    :returns: The resolved, absolute sandbox root.
    """
    base = os.environ.get("OMNIGENT_RUNNER_WORKSPACE") or os.getcwd()
    if spec_cwd:
        path = pathlib.Path(spec_cwd)
        if not path.is_absolute():
            path = pathlib.Path(base) / path
    else:
        path = pathlib.Path(base)
    return path.resolve(strict=False)


def prepare_claude_cli_path(
    real_cli_path: str | None,
    spec: OSEnvSpec | None,
) -> PreparedClaudeCli:
    """Wrap the Claude CLI in the agent's configured sandbox when possible.

    Degrades instead of crashing: when the sandbox can't be resolved or
    the wrap can't be built (unsupported platform, un-grantable
    interpreter layout, profile-size overflow), the CLI is returned
    unwrapped with native tools disabled — the same confinement story
    as ``OMNIGENT_CLAUDE_SDK_NO_SANDBOX``: file/shell access still goes
    through the independently sandboxed ``sys_os_*`` helpers (which
    fail closed on their own), and only the CLI supervisor process
    runs unwrapped.

    :param real_cli_path: Absolute path to the system-installed Claude CLI
        binary, or ``None`` when no CLI is available.
    :param spec: The agent's ``os_env`` spec.  Only ``caller_process`` specs
        with a compatible sandbox are eligible for wrapping.
    :returns: A :class:`PreparedClaudeCli` naming the effective CLI path and
        whether native tools should be enabled.
    """

    if real_cli_path is None or spec is None or spec.type != "caller_process":
        return PreparedClaudeCli(cli_path=real_cli_path, enable_native_tools=False)

    if _sandbox_disabled_by_env():
        return PreparedClaudeCli(cli_path=real_cli_path, enable_native_tools=False)

    sandbox_spec = spec.sandbox or OSEnvSandboxSpec()
    if sandbox_spec.type == "none":
        return PreparedClaudeCli(cli_path=real_cli_path, enable_native_tools=True)

    cwd = _resolve_sandbox_cwd(spec.cwd)
    try:
        sandbox = resolve_sandbox(spec, cwd)
    except (OSError, NotImplementedError) as exc:
        logger.warning(
            "Cannot resolve the configured sandbox for the Claude CLI wrap; "
            "running the CLI unwrapped with native tools disabled "
            "(file/shell access stays confined to the sandboxed sys_os_* "
            "tools): %s",
            exc,
        )
        return PreparedClaudeCli(cli_path=real_cli_path, enable_native_tools=False)
    if not sandbox.active:
        return PreparedClaudeCli(cli_path=real_cli_path, enable_native_tools=False)
    if not sandbox.allow_network:
        # The Claude CLI itself must reach the provider, so we cannot run the
        # whole native-tool process tree inside a network-denying sandbox.
        return PreparedClaudeCli(cli_path=real_cli_path, enable_native_tools=False)

    sandbox = with_additional_read_roots(sandbox, _claude_internal_write_roots())
    sandbox = with_additional_write_roots(sandbox, _claude_internal_write_roots())
    sandbox = with_additional_write_files(sandbox, _claude_internal_write_files())
    # Dry-run the spawn-time wrap now, while degrading is still possible.
    # The real wrap happens later inside run_launcher, where an OSError
    # (un-grantable interpreter layout, profile-size cap, cwd-scan
    # overflow) kills the launcher and surfaces as an opaque connect
    # timeout. By run time native tools are already enabled, so this is
    # the last point where "skip the wrap" is still safe.
    try:
        get_backend(sandbox.backend_type).wrap_launcher_argv(
            [sys.executable, "-c", "pass"], sandbox, cwd, target=real_cli_path
        )
    except OSError as exc:
        logger.warning(
            "The configured sandbox cannot wrap the Claude CLI at %s; "
            "running it unwrapped with native tools disabled (file/shell "
            "access stays confined to the sandboxed sys_os_* tools). "
            "Remediation hints in the underlying error: %s",
            real_cli_path,
            exc,
        )
        return PreparedClaudeCli(cli_path=real_cli_path, enable_native_tools=False)
    return PreparedClaudeCli(
        cli_path=create_exec_launcher(real_cli_path, sandbox),
        enable_native_tools=True,
    )


def prepare_tight_cli_process_path(
    real_cli_path: str | None,
    *,
    cwd: str | None = None,
) -> str | None:
    """Wrap the Claude CLI in a tight default sandbox without enabling tools."""

    if real_cli_path is None:
        return None

    if _sandbox_disabled_by_env():
        return real_cli_path

    # Skip silently on non-Linux: the implicit default sandbox here is
    # ``linux_bwrap`` and ``resolve_sandbox`` would raise
    # NotImplementedError / OSError on every macOS / Windows run. The
    # operator's only recourse is to either accept the no-op or
    # set ``os_env.sandbox.type='none'`` explicitly — both
    # already produce the same behavior we land on here, so
    # logging a warning every run is just noise (and breaks
    # tests that assert ``stderr_is_clean``).
    if sys.platform != "linux":
        return real_cli_path

    spec = OSEnvSpec(
        type="caller_process",
        cwd=cwd,
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap",
            write_paths=[],
            allow_network=True,
        ),
    )
    try:
        resolved_cwd = _resolve_sandbox_cwd(cwd)
        sandbox = resolve_sandbox(spec, resolved_cwd)
    except (OSError, NotImplementedError) as exc:
        logger.warning(
            "Could not apply default local CLI sandbox; continuing without it: %s",
            exc,
        )
        return real_cli_path
    else:
        if not sandbox.active:
            return real_cli_path
        sandbox = with_additional_write_roots(sandbox, _claude_internal_write_roots())
        sandbox = with_additional_write_files(sandbox, _claude_internal_write_files())
        return create_exec_launcher(real_cli_path, sandbox)


@dataclass(frozen=True)
class _ResolvedSkills:
    """
    Pair of SDK options derived from a single ``skills_filter``
    value: ``ClaudeAgentOptions.skills`` and
    ``ClaudeAgentOptions.setting_sources``.

    Both are needed because the SDK's ``_apply_skills_defaults``
    auto-defaults ``setting_sources`` to ``["user", "project"]``
    whenever ``skills`` is non-None — including when ``skills=[]``.
    That auto-default loads ``~/.claude/skills/`` and the cwd's
    ancestor ``.claude/skills/`` chain into the system prompt
    listing even when the ``Skill`` tool itself is suppressed.
    Hermetic agents need to explicitly override
    ``setting_sources=[]`` to actually hide host skills from the
    model's view of its own skill listing.

    :param skills: Value for ``ClaudeAgentOptions.skills``:
        ``"all"`` / list of names / empty list for hermetic mode.
    :param setting_sources: Value for
        ``ClaudeAgentOptions.setting_sources``: ``None`` to let
        the SDK pick its default (``["user", "project"]``), or
        an explicit list (e.g. ``[]`` for hermetic mode where we
        don't want any scope-based discovery).
    """

    skills: str | list[str]
    setting_sources: list[str] | None


def _resolve_skills_option(
    skills_filter: str | list[str],
) -> _ResolvedSkills | None:
    """
    Translate the spec's ``skills_filter`` into the pair of SDK
    options ``ClaudeAgentOptions.skills`` and
    ``ClaudeAgentOptions.setting_sources``.

    Three meaningful filter values produce three distinct SDK
    configurations:

    - ``"all"`` → ``skills="all"``, ``setting_sources=None`` (SDK
      auto-defaults to ``["user", "project"]``). All host skills
      from ``~/.claude/skills/`` and ``<cwd>/.claude/skills/``
      (walking up the cwd tree) appear in the model's listing.
    - ``"none"`` → ``skills=[]``, ``setting_sources=[]``. Both
      the ``Skill`` tool listing AND the scope-based discovery
      are suppressed: no host skills appear in the system
      prompt or as invokable. Bundled skills (loaded via
      ``--plugin-dir``) are unaffected by ``setting_sources``
      and remain visible.
    - ``list[str]`` → ``skills=[names]``, ``setting_sources=None``.
      Only the named subset is in the model's listing; the SDK's
      auto-default still loads user and project sources for
      CLAUDE.md and other settings.

    :param skills_filter: ``"all"`` / ``"none"`` / list of skill
        names from :class:`AgentSpec.skills_filter`.
    :returns: The :class:`_ResolvedSkills` pair, or ``None`` when
        *skills_filter* is malformed — the caller falls back to
        ``"all"`` semantics.
    """
    if skills_filter == "all":
        return _ResolvedSkills(skills="all", setting_sources=None)
    if skills_filter == "none":
        # Empty ``skills`` suppresses the listing AND empty
        # ``setting_sources`` skips the SDK's auto-default that
        # would otherwise load ``~/.claude/skills/`` for the
        # system prompt anyway.
        return _ResolvedSkills(skills=[], setting_sources=[])
    if isinstance(skills_filter, list):
        return _ResolvedSkills(skills=list(skills_filter), setting_sources=None)
    return None


class ClaudeSDKExecutor(Executor):
    """Execute agent turns using the Claude Agent SDK.

    The SDK runs Claude Code's full agent loop internally.  Omnigent tools
    are registered as MCP tools so the model can call them.  Built-in OS tools
    (Bash, Read, Edit, …) are only enabled when the agent's ``os_env`` flag
    is set.  Even without ``os_env``, Omnigent tries to place the Claude CLI
    itself in a tight default sandbox on supported Linux hosts.

    The SDK manages its own tool-call loop.  This executor yields events
    reconstructed from the SDK message stream:
    - ToolCallRequest for each ToolUseBlock (for history building)
    - TextChunk for streaming text
    - TurnComplete with the final result

    Multi-turn: call ``run_turn()`` repeatedly.  The executor maintains a
    persistent ``ClaudeSDKClient`` that preserves conversation context across
    turns by keeping a live Claude SDK client per Omnigent session.

    Gateway support: pass ``gateway=True`` to route through a vendor-neutral
    gateway (base URL + bearer-token command + model) supplied by a
    ``key`` / ``gateway`` provider.
    """

    def __init__(
        self,
        *,
        cwd: str | None = None,
        os_env: OSEnvSpec | None = None,
        model: str | None = None,
        permission_mode: str = "auto",
        gateway: bool = False,
        gateway_host: str | None = None,
        base_url_override: str | None = None,
        gateway_auth_command: str | None = None,
        gateway_auth_refresh_interval_ms: str | None = None,
        retry_policy: RetryPolicy | None = None,
        bundle_dir: pathlib.Path | None = None,
        agent_name: str | None = None,
        skills_filter: str | list[str] = "all",
        api_key_helper: str | None = None,
        native_skills: bool = True,
    ) -> None:
        """Create a ClaudeSDKExecutor.

        Args:
            cwd: Working directory for Claude Code.
            os_env: If set, enable built-in OS tools (Bash, Read, Edit, …)
                and align them with the provided OS environment. When the
                spec's sandbox is enabled, Omnigent wraps the Claude CLI
                in the same sandbox. If omitted, Omnigent still tries to
                sandbox the Claude CLI process itself on supported Linux
                hosts, but does not enable native OS tools.
            model: Override the model name.
            permission_mode: SDK permission mode (default: auto
                so the agent runs autonomously with background safety checks).
            gateway: If True, route through a vendor-neutral gateway
                (base URL + bearer-token command + model).
            gateway_host: Gateway host origin, e.g.
                ``"https://gateway.example.com"``.  Set from
                ``HARNESS_CLAUDE_SDK_GATEWAY_HOST`` (written by the AP
                workflow layer). When set, requires the gateway base URL
                and auth command values.
            base_url_override: The gateway's Anthropic base URL.  Set from
                ``HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL`` (written by the AP
                workflow layer). Required whenever ``gateway_host`` is set.
            gateway_auth_command: Shell command that prints a bearer token,
                e.g. ``"printf %s sk-or-..."``. Set from
                ``HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND``.
            gateway_auth_refresh_interval_ms: Refresh TTL as a string,
                e.g. ``"900000"``. Set from
                ``HARNESS_CLAUDE_SDK_GATEWAY_AUTH_REFRESH_INTERVAL_MS``.
            bundle_dir: Materialized agent-bundle root, when the agent
                ships its own ``skills/`` directory. Used to expose
                bundled skills to Claude via ``--plugin-dir <bundle>``
                (the SDK's plugin convention loads SKILL.md files from
                ``<plugin>/skills/<dir>/``). ``None`` for agents
                without a bundled-skill directory — the harness skips
                the plugin-dir wiring.
            agent_name: Optional agent display name. When *bundle_dir*
                is set, used to write a one-line ``.claude-plugin/
                plugin.json`` manifest so bundled skills get clean
                ``<agent-name>:<skill-name>`` namespaced labels in
                Claude's skill listing (instead of being labeled by
                the bundle's tmp-dir basename).
            skills_filter: Host-skill filter (``"all"`` / ``"none"`` /
                ``list[str]``). Maps to the SDK's ``skills`` option:
                ``"all"`` → enable every host-discovered skill,
                ``"none"`` → empty list (no host skills exposed),
                list of names → only the named skills. Bundled
                skills loaded via ``bundle_dir`` are subject to the
                same listing filter (so ``"none"`` hides every skill
                from the model, bundled or host); agents that want
                bundled skills always visible while opting out of
                host skills should set this to a list naming their
                bundled skills explicitly. Defaults to ``"all"``.
            api_key_helper: Shell command the Claude CLI will invoke to
                retrieve a bearer token, e.g.
                ``"printf %s sk-ant-..."`` (set by the harness when
                ``executor.auth: {type: api_key, …}`` is declared).
                Injected into ``_extra_env`` as
                :data:`_CLAUDE_API_KEY_HELPER_ENV_KEY` so it reaches
                the SDK's ``settings.apiKeyHelper`` option at turn time.
        """
        self._cwd = cwd
        self._os_env_spec = os_env
        self._os_env = os_env is not None
        self._model_override = model
        self._permission_mode = permission_mode
        self._gateway = gateway
        self._gateway_host = gateway_host.rstrip("/") if gateway_host else None
        self._base_url_override = base_url_override
        self._gateway_auth_command = gateway_auth_command
        self._gateway_auth_refresh_interval_ms = _parse_optional_int(
            gateway_auth_refresh_interval_ms
        )
        self._bundle_dir = bundle_dir
        self._agent_name = agent_name
        self._skills_filter = skills_filter
        # ``False`` (a spec's ``executor.config.native_skills: false``, every Super Chat bundle):
        # no Claude Code ``Skill`` tool, skill listing or bundle plugin; skills are Omnigent's.
        self._native_skills = native_skills
        # Write the bundle's plugin manifest now (idempotent) so that
        # ``--plugin-dir <bundle>`` produces clean
        # ``<agent-name>:<skill-name>`` labels in Claude's skill
        # listing instead of the auto-derived tmpdir basename.
        if self._bundle_dir is not None:
            try:
                ensure_bundle_plugin_manifest(self._bundle_dir, self._agent_name)
            except OSError as exc:
                logger.warning(
                    "could not write bundle plugin manifest at %s: %s",
                    self._bundle_dir,
                    exc,
                )
        self._tool_executor: ToolExecutor | None = None
        # Elicitation handler wired in by ExecutorAdapter. When set
        # (and permission_mode is not bypassPermissions), each tool
        # call is gated by an async approve/deny round-trip through
        # the Omnigent elicitation system rather than silently allowed.
        # ``None`` until the adapter installs it on first use.
        self._elicitation_handler: ElicitationHandler | None = None
        # Live Claude SDK clients keyed by Omnigent session id.
        self._clients: dict[str, _ClaudeClientState] = {}
        # Per live turn: the window in which a mid-turn message may be pushed.
        self._steer_windows: dict[str, _SteerWindow] = {}
        self._pending_framework_context: dict[str, str] = {}
        # ``tool_use_id`` of each Omnigent MCP call the CLI is about to run, from the
        # PreToolUse hook, until its handler claims it: tool_use_id -> (name, args json).
        self._unclaimed_tool_use_ids: dict[str, tuple[str, str]] = {}
        # Session keys whose Claude harness process crashed and must not be reused.
        self._crashed_sessions: dict[str, str] = {}
        # Session keys interrupted during their current turn: the turn ends as cancelled.
        self._interrupted_sessions: set[str] = set()
        # Force-close tasks for clients evicted on turn cancellation, kept
        # referenced so they are not GC'd mid-close.
        self._cancel_close_tasks: set[asyncio.Task[None]] = set()

        # Prefer system-installed claude over the SDK's bundled CLI.
        # The bundled CLI may be older and send beta flags that a
        # gateway may not support.
        self._cli_wrapper_path: str | None = None
        self._cli_path: str | None = _find_system_claude()
        if self._cli_path:
            if self._os_env_spec is not None:
                prepared = prepare_claude_cli_path(
                    self._cli_path,
                    self._os_env_spec,
                )
                self._os_env = prepared.enable_native_tools
                if prepared.cli_path != self._cli_path:
                    self._cli_wrapper_path = prepared.cli_path
                    self._cli_path = prepared.cli_path
            else:
                wrapped_cli = prepare_tight_cli_process_path(
                    self._cli_path,
                    cwd=self._cwd,
                )
                if wrapped_cli != self._cli_path:
                    self._cli_wrapper_path = wrapped_cli
                    self._cli_path = wrapped_cli
            if self._os_env_spec is not None and self._cwd is None:
                self._cwd = self._os_env_spec.cwd
            logger.info("Using system claude CLI: %s", self._cli_path)
        else:
            logger.info("No system claude found; SDK will use bundled CLI")

        # Eagerly resolve the gateway transport env so errors surface at
        # construction time.
        self._extra_env: dict[str, str] = {}
        if gateway:
            gateway_env = _resolve_gateway_env(
                host_override=self._gateway_host,
                base_url_override=base_url_override,
                auth_command_override=self._gateway_auth_command,
                auth_refresh_interval_ms=self._gateway_auth_refresh_interval_ms,
            )
            if not gateway_env:
                raise OSError(
                    "ClaudeSDKExecutor(gateway=True) requires gateway credentials "
                    "from the gateway base URL / auth command."
                )
            self._extra_env.update(gateway_env)
        self._extra_env[_CLAUDE_CODE_ENABLE_TOOL_SEARCH_ENV] = "true"

        # Retry policy → Anthropic SDK env vars passed to the Claude
        # CLI subprocess. ``ANTHROPIC_MAX_RETRIES`` and
        # ``ANTHROPIC_REQUEST_TIMEOUT_SECONDS`` are speculative — the
        # CLI's retry budget isn't publicly documented as env-tunable.
        # See ``RetryPolicy.claude_cli.env()``.
        self._retry_policy = retry_policy if retry_policy is not None else RetryPolicy()
        self._extra_env.update(self._retry_policy.claude_cli.env())

        # api_key_helper: shell command the Claude CLI invokes to retrieve a
        # bearer token (``executor.auth: {type: api_key, …}`` path).  Must
        # be injected here (not read from os.environ) because
        # ``_get_or_create_client`` strips ``ANTHROPIC_API_KEY`` from
        # os.environ before connecting, and the executor reads
        # ``_CLAUDE_API_KEY_HELPER_ENV_KEY`` from ``_extra_env`` only.
        if api_key_helper:
            self._extra_env[_CLAUDE_API_KEY_HELPER_ENV_KEY] = api_key_helper

    def __del__(self) -> None:
        wrapper_path = getattr(self, "_cli_wrapper_path", None)
        if isinstance(wrapper_path, str):
            with suppress(Exception):
                pathlib.Path(wrapper_path).unlink(missing_ok=True)

    async def _get_or_create_client(
        self,
        sdk: _ClaudeSDK,
        *,
        session_key: str,
        options: SdkOptions,
        model: str | None,
        rebuild_on_instructions_change: bool = False,
        launch_key: dict[str, str] | None = None,
        primed: bool = True,
    ) -> _ClaudeClient:
        state = self._clients.get(session_key)
        if state is None:
            reason = _client_drop_reasons.pop(session_key, None) or (
                "pre-launch" if not primed else "no warm client in this harness process"
            )
        else:
            reason = ""
        if (
            state is not None
            and not getattr(state, "primed", True)
            and state.launch_key != launch_key
        ):
            # Launched ahead of this turn with other options (tools, prompt, effort):
            # it has seen no turn yet, so relaunching loses nothing.
            differs = sorted(
                name
                for name in {**(state.launch_key or {}), **(launch_key or {})}
                if (state.launch_key or {}).get(name) != (launch_key or {}).get(name)
            )
            reason = f"the pre-launched CLI differs from the first turn in {', '.join(differs)}"
            await self._close_live_client(session_key)
            state = None
        if (
            state is not None
            and state.launch_key is not None
            and launch_key is not None
            and state.launch_key.get("mode") != launch_key.get("mode")
        ):
            # The mode (context mode, turn cap) is fixed in the CLI's env and options at
            # launch; a warm CLI keeps the one it started with.
            reason = "the turn's mode changed"
            await self._close_live_client(session_key)
            state = None
        new_instructions_hash = (
            _composed_instructions_hash(getattr(options, "system_prompt", None))
            if rebuild_on_instructions_change
            else None
        )
        if (
            state is not None
            and rebuild_on_instructions_change
            and new_instructions_hash != state.instructions_hash
        ):
            # A warm client keeps the ``system_prompt`` it was created with
            # (the SDK never re-sends it); for superside-chat, framework
            # instructions composed after turn 1 (memory, rollover) would
            # otherwise never reach the model. Drop and reconnect so the
            # next turn's options actually apply.
            reason = "the composed instructions changed"
            await self._close_live_client(session_key)
            state = None
        if state is None:
            launch_started = time.monotonic()
            # Tee CLI stderr so the connect timeout error carries the
            # tail; ``_on_stderr`` alone only logs at DEBUG.
            connect_stderr: list[str] = []
            original_stderr = getattr(options, "stderr", None)

            def _tee_stderr(line: str) -> None:
                connect_stderr.append(line)
                if original_stderr is not None:
                    original_stderr(line)

            options.stderr = _tee_stderr
            client = sdk.ClaudeSDKClient(options)
            try:
                # CLAUDECODE must be absent (not just empty) in the child
                # env — otherwise the claude cli reports a nested-session
                # error. The SDK merges ``os.environ`` with ``options.env``,
                # so we unset in ``os.environ`` for the spawn window.
                #
                # ANTHROPIC_API_KEY is also stripped so the CLI uses its
                # subscription auth rather than a developer API key that
                # would charge separately. Safe in gateway mode too:
                # ``options.settings`` explicitly sets apiKeyHelper and
                # ``options.env`` sets the gateway base URL, so the
                # Claude CLI does not need an inherited Anthropic key.
                with ExitStack() as env_stack:
                    env_stack.enter_context(_unset_env_var("CLAUDECODE"))
                    env_stack.enter_context(_unset_env_var("ANTHROPIC_API_KEY"))
                    await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT_SECONDS)
            except asyncio.CancelledError:
                # Stopped mid-launch (an interrupted pre-launch): close the half-started CLI
                # in the background, since an await here would itself be cancelled.
                closing = asyncio.create_task(self._force_close_client(client))
                self._cancel_close_tasks.add(closing)
                closing.add_done_callback(self._cancel_close_tasks.discard)
                raise
            except asyncio.TimeoutError as exc:
                await self._force_close_client(client)
                tail = "\n".join(line.rstrip() for line in connect_stderr[-40:])
                detail = tail or "(no CLI stderr captured)"
                logger.warning(
                    "Claude SDK connect timed out after %ds; CLI stderr tail:\n%s",
                    int(_CONNECT_TIMEOUT_SECONDS),
                    detail,
                )
                raise TimeoutError(
                    f"Claude SDK client connect timed out after "
                    f"{int(_CONNECT_TIMEOUT_SECONDS)}s. CLI stderr tail:\n{detail}"
                ) from exc
            except Exception as exc:
                # The CLI may exit immediately (e.g. ``unknown option``) before
                # the SDK's async stderr reader has flushed all output.  A brief
                # yield lets pending reader callbacks deliver remaining lines so
                # the error message is useful rather than just "Command failed
                # with exit code 1; Check stderr output for details".
                await asyncio.sleep(0.1)
                await self._force_close_client(client)
                tail = "\n".join(line.rstrip() for line in connect_stderr[-40:])
                detail = tail or "(no CLI stderr captured)"
                logger.error(
                    "Claude SDK connect failed: %s\nCLI stderr tail:\n%s",
                    exc,
                    detail,
                )
                raise RuntimeError(
                    f"Claude SDK connect failed: {exc}\nCLI stderr:\n{detail}"
                ) from exc
            finally:
                # Restore on both paths so post-connect stderr flows
                # directly to the original callback and ``connect_stderr``
                # can be GC'd instead of growing for the session lifetime.
                options.stderr = original_stderr
            current_task: asyncio.Task[None] | None = cast(
                "asyncio.Task[None] | None", asyncio.current_task()
            )
            state = _ClaudeClientState(
                client=client,
                model=model,
                loop=asyncio.get_running_loop(),
                task=current_task,
                instructions_hash=new_instructions_hash,
                primed=primed,
                launch_key=launch_key,
            )
            self._clients[session_key] = state
            _client_drop_reasons.pop(session_key, None)
            logger.info(
                "Claude CLI launched for session %s in %.2fs: %s",
                session_key,
                time.monotonic() - launch_started,
                reason,
            )
            return client

        if state.model != model:
            await state.client.set_model(model)
            state.model = model
        return state.client

    async def close_session(self, session_key: str) -> None:
        self._crashed_sessions.pop(session_key, None)
        await self._close_live_client(session_key)

    async def _close_live_client(self, session_key: str, reason: str | None = None) -> None:
        state = self._clients.pop(session_key, None)
        if state is None:
            return
        if reason is not None:
            _client_drop_reasons[session_key] = reason
        try:
            current_loop = asyncio.get_running_loop()
            current_task = asyncio.current_task()
        except RuntimeError:
            current_loop = None
            current_task = None

        same_loop = state.loop is None or current_loop is state.loop
        same_task = state.task is None or current_task is state.task
        if not (same_loop and same_task):
            logger.debug(
                "Force-closing Claude SDK client for session %s (different event loop/task; "
                "expected once the connecting turn has finished, e.g. idle reap / shutdown)",
                session_key,
            )
            await self._force_close_client(state.client)
            return
        try:
            await state.client.disconnect()
        except RuntimeError as exc:
            if "different task" not in str(exc):
                raise
            logger.debug(
                "Force-closing Claude SDK client for session %s (different task; "
                "expected once the connecting turn has finished, e.g. idle reap / shutdown)",
                session_key,
            )
            await self._force_close_client(state.client)
            return
        # SDK's disconnect() doesn't close the asyncio transport;
        # call _force_close_client to flip transport._closed before
        # the loop tears down.
        await self._force_close_client(state.client)

    def _evict_client_on_cancel(self, session_key: str) -> None:
        state = self._clients.pop(session_key, None)
        if state is not None:
            _client_drop_reasons[session_key] = "the previous turn was cancelled"
        if state is None:
            return
        # Close in the background: an await here runs under an in-flight
        # cancellation and could itself be cancelled, leaking the CLI process.
        task = asyncio.create_task(self._force_close_client(state.client))
        self._cancel_close_tasks.add(task)
        task.add_done_callback(self._cancel_close_tasks.discard)

    async def close(self) -> None:
        session_keys = list(self._clients)
        for session_key in session_keys:
            await self.close_session(session_key)

    async def interrupt_session(self, session_key: str) -> bool:
        state = self._clients.get(session_key)
        if state is None:
            return False
        self._interrupted_sessions.add(session_key)
        # Interrupt is best-effort and fast; a failure just falls through to
        # the close below.
        try:
            await asyncio.wait_for(state.client.interrupt(), timeout=0.5)
        except Exception as exc:  # noqa: BLE001 — interrupt is best-effort
            logger.warning(
                "Claude SDK interrupt failed for session %s: %s",
                session_key,
                exc,
            )
        # Always drop the live session after an interrupt. Its transcript
        # still holds the abandoned prompt, and resumed turns send only the
        # latest user message (see _build_prompt), so the runner's
        # "[System: interrupted]" marker would never reach the model. Closing
        # forces the next turn to rebuild full history (marker included) in a
        # fresh session — the abandoned request is then visible-but-superseded
        # rather than silently continued.
        _client_drop_reasons[session_key] = "the previous turn was interrupted"
        try:
            await self.close_session(session_key)
            return True
        except Exception as exc:  # noqa: BLE001 — close failures surface via False return
            logger.warning(
                "Claude SDK session close after interrupt failed for session %s: %s",
                session_key,
                exc,
            )
            return False

    async def steer_session_message(
        self,
        session_key: str,
        text: str,
        *,
        steer_id: str,
    ) -> SteerOffer:
        """Push a message into the turn that is running now (a steer).

        The CLI is in streaming-input mode, so a user message written to its stdin while a
        turn runs joins the CLI's command queue at priority ``next``: it is attached to the
        running turn at the next tool boundary (or, when it misses the turn, run as the CLI's
        own next turn). Either way the CLI echoes it with our ``uuid``
        (``--replay-user-messages``), which :meth:`_receive_with_steers` turns into a
        :class:`SteerOutcome`. Answers ``"refused"`` when no turn is running, its stream has
        finished, or the write failed.
        """
        window = self._steer_windows.get(session_key)
        state = self._clients.get(session_key)
        if window is None or not window.open or state is None:
            return "refused"
        if not text:
            return "refused"
        message_uuid = str(uuid.uuid4())
        # Registered before the write so the stream reads on until the CLI answers for it.
        window.pending[message_uuid] = steer_id
        # Shielded: a write cut in half could leave the CLI holding a message nobody tracks.
        write = asyncio.ensure_future(
            state.client.query(
                _steer_message_iter(text, session_id=session_key, message_uuid=message_uuid),
                session_id=session_key,
            )
        )
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            # The write finishes regardless; its echo (or the stream's end) settles it.
            raise
        except Exception as exc:  # noqa: BLE001 -- a failed write is a refusal, not a turn failure
            window.pending.pop(message_uuid, None)
            logger.warning("Claude SDK steer write failed for session %s: %s", session_key, exc)
            # A broken stdin means the CLI is gone: drop it so the turn's stream ends too.
            await self._close_live_client(session_key, "a steer write failed")
            return "refused"
        return "pending"

    async def _receive_with_steers(
        self,
        sdk: _ClaudeSDK,
        client: _ClaudeClient,
        window: _SteerWindow,
    ) -> AsyncIterator[Any]:  # type: ignore[explicit-any]
        """Yield the CLI's messages for a turn, through any steers pushed into it.

        The CLI echoes every message it takes up with our uuid: attached at a tool boundary
        (``queued_command``) or run as its own next turn. Each echo becomes a
        ``SteerOutcome(taken=True)``. ``receive_response()`` stops at a ``ResultMessage``, so
        while a pushed message is unanswered the stream reads on: the CLI runs it next and
        echoes it, or its stream ends. The window closes in the same step in which nothing is
        outstanding (no ``await`` between), so a later steer is refused instead of written to
        a CLI nobody reads. Messages still pending when the stream ends are settled by the
        caller (see :meth:`_settle_steer_window`).
        """
        while True:
            stream = client.receive_response()
            saw_result = False
            try:
                async for message in stream:
                    if isinstance(message, sdk.UserMessage):
                        steer_id = window.pending.pop(getattr(message, "uuid", None) or "", None)
                        if steer_id is not None:
                            yield SteerOutcome(steer_id=steer_id, taken=True)
                            continue
                    if isinstance(message, sdk.ResultMessage):
                        saw_result = True
                    yield message
            finally:
                aclose = getattr(stream, _ACLOSE_ATTR, None)
                if aclose is not None:
                    await aclose()
            if saw_result and window.pending:
                continue
            window.open = False
            return

    @staticmethod
    def _settle_steer_window(session_key: str, window: _SteerWindow | None) -> list[SteerOutcome]:
        """Close a turn's steer window; refuse what the CLI never answered for.

        A message still pending means the stream ended without the CLI finishing (no result
        after it): the CLI may hold it, so the turn's ``finally`` (or its error path) has
        already dropped the live client, and the next turn starts fresh with the caller's
        own copy.
        """
        if window is None:
            return []
        window.open = False
        refused = [
            SteerOutcome(steer_id=steer_id, taken=False) for steer_id in window.pending.values()
        ]
        window.pending.clear()
        if refused:
            logger.warning(
                "Claude SDK turn for session %s ended with %d steer(s) unanswered",
                session_key,
                len(refused),
            )
        return refused

    @staticmethod
    async def _force_close_client(client: _ClaudeClient) -> None:
        # getattr defensively: runs on the success path too, so must
        # no-op against test fakes that only stub `disconnect`.
        query = getattr(client, "_query", None)
        if query is not None:
            # Cancel the SDK's reader before marking the query closed: a reader that sees
            # ``_closed`` breaks out of the transport's message generator mid-yield, and that
            # generator's finalizer later raises the CLI's exit status in a task nobody awaits.
            read_task = getattr(query, "_read_task", None)
            if read_task is not None:
                with suppress(Exception):
                    read_task.cancel()
            query._closed = True
            tg = getattr(query, "_tg", None)
            if tg is not None:
                with suppress(Exception):
                    tg.cancel_scope.cancel()

        transport = getattr(client, "_transport", None)
        if transport is not None:
            # The SDK's stderr reader changed shape across revs: current
            # claude-agent-sdk (>=0.2.x) exposes a single ``_stderr_task``
            # TaskHandle with ``cancel()``; older revs an anyio
            # ``_stderr_task_group``. Probe both via getattr so a force-close
            # never raises AttributeError out of lifespan shutdown (which
            # crashed the runner on session stop).
            stderr_task = getattr(transport, "_stderr_task", None)
            if stderr_task is not None:
                with suppress(Exception):
                    stderr_task.cancel()
            else:
                stderr_tg = getattr(transport, "_stderr_task_group", None)
                if stderr_tg is not None:
                    with suppress(Exception):
                        stderr_tg.cancel_scope.cancel()

            for stream in (
                transport._stdout_stream,
                transport._stdin_stream,
                transport._stderr_stream,
            ):
                if stream is not None:
                    _best_effort_close(stream)

            process = transport._process
            if process is not None:
                if process.returncode is None:
                    _terminate_process_tree(process)
                    try:
                        with suppress(Exception):
                            await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        _kill_process_tree(process)
                        with suppress(Exception):
                            await process.wait()
                    except asyncio.CancelledError:
                        _kill_process_tree(process)
                        with suppress(Exception):
                            await process.wait()

                _best_effort_close(process)
                # The SDK's `transport._process` is an anyio Process;
                # `_best_effort_close` can't reach its asyncio
                # transport (no sync `close`). Close it explicitly.
                close_anyio_subprocess_transport(process)

            transport._process = None
            transport._stdout_stream = None
            transport._stdin_stream = None
            transport._stderr_stream = None
            if getattr(transport, "_stderr_task", None) is not None:
                transport._stderr_task = None
            if getattr(transport, "_stderr_task_group", None) is not None:
                transport._stderr_task_group = None
            transport._ready = False

        client._query = None
        client._transport = None

    def supports_streaming(self) -> bool:
        return True

    def supports_tool_calling(self) -> bool:
        return True

    def handles_tools_internally(self) -> bool:
        return True

    def supports_live_message_queue(self) -> bool:
        # A message written to the CLI's stdin mid-turn is taken up by the running turn
        # (see ``steer_session_message``); ``_receive_with_steers`` reads every answer for it.
        return True

    def supports_tool_boundary_interrupt(self) -> bool:
        return True

    def max_context_tokens(self) -> int | None:
        return None  # SDK manages its own context

    def _session_key(self, messages: list[Message]) -> str:
        for msg in reversed(messages):
            session_id = msg.get("session_id")
            if session_id:
                return str(session_id)
            metadata = msg.get("metadata", {})
            if isinstance(metadata, dict) and metadata.get("session_id"):
                return str(metadata["session_id"])
        return "default"

    async def _apply_gateway_model_vocabulary(
        self, env: dict[str, str], auth_command: str | None
    ) -> dict[str, str]:
        """
        Pin the family aliases in *env* and report Claude Code's id rewrites.

        On a gateway transport, list the gateway's models once and derive both
        halves of Claude Code's model vocabulary from that listing: the
        ``ANTHROPIC_DEFAULT_*_MODEL`` pins that resolve a family alias to a
        served id, and the canonical-to-served rewrites for the ids the CLI
        names on its own (its refusal-fallback route table). A no-op off the
        gateway, where a direct Anthropic endpoint speaks canonical ids itself.

        Pins already present are respected. The rewrites still apply to those
        launches: pinning an alias does not teach Claude Code how this gateway
        spells the canonical ids its own route table names.

        :param env: The child-process env dict, mutated in place with the pins.
        :param auth_command: The gateway ``apiKeyHelper`` command used to mint
            a bearer for the model listing.
        :returns: Canonical-to-served rewrites for the ``modelOverrides``
            setting. Empty off the gateway or when discovery found nothing.
        """
        base_url = env.get("ANTHROPIC_BASE_URL")
        if not self._gateway or not base_url:
            return {}
        if urlparse(base_url).hostname == _DIRECT_ANTHROPIC_HOST:
            return {}
        vocabulary = await _cached_gateway_vocabulary(base_url, auth_command)
        for var, model_id in vocabulary.alias_pins.items():
            env.setdefault(var, model_id)
        return vocabulary.model_overrides

    def _install_framework_context_hook(
        self, sdk: _ClaudeSDK, options: SdkOptions, session_key: str
    ) -> None:
        """Read current-turn context even when the SDK reuses its original hooks."""
        hook_matcher = getattr(sdk, "HookMatcher", None)
        if hook_matcher is None:
            return

        async def add_context(
            _payload: object, _tool_use_id: str | None, _context: object
        ) -> _JsonObject:
            text = self._pending_framework_context.pop(session_key, "")
            if not text:
                return {}
            return {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": text,
                }
            }

        hooks = dict(getattr(options, "hooks", None) or {})
        hooks.setdefault("UserPromptSubmit", []).append(hook_matcher(hooks=[add_context]))
        options.hooks = hooks

    def _install_ends_turn_hook(self, sdk: _ClaudeSDK, options: SdkOptions) -> None:
        """End the turn after a tool batch in which a card tool asked to (``ends_turn``).

        The CLI's own stop: a ``PostToolBatch`` hook answering ``continue: false`` returns
        from the turn once every call in the batch has resolved, before the next model call
        and before queued input is attached, so a steer waiting in the CLI's queue runs as its
        next turn. Nothing is interrupted and the warm history stays clean.
        """
        hook_matcher = getattr(sdk, "HookMatcher", None)
        if hook_matcher is None:
            return

        async def stop_after_card(
            payload: object, _tool_use_id: str | None, _context: object
        ) -> _JsonObject:
            calls = payload.get("tool_calls") if isinstance(payload, dict) else None
            if not isinstance(calls, list):
                return {}
            if any(
                isinstance(call, dict)
                and result_ends_turn(str(call.get("tool_name") or ""), call.get("tool_response"))
                for call in calls
            ):
                return {"continue_": False, "stopReason": "Waiting for the person's answer."}
            return {}

        hooks = dict(getattr(options, "hooks", None) or {})
        hooks.setdefault("PostToolBatch", []).append(hook_matcher(hooks=[stop_after_card]))
        options.hooks = hooks

    def _install_tool_use_id_hook(self, sdk: _ClaudeSDK, options: SdkOptions) -> None:
        """Note the ``tool_use_id`` of each Omnigent MCP call before the CLI runs it.

        The CLI asks this hook before it sends the MCP call, so the id is always here when
        the handler runs, even while the stream that announces the call is still queued
        behind long streamed arguments. Without it the dispatch minted its own id and the
        call was stored twice, once under each id.
        """
        hook_matcher = getattr(sdk, "HookMatcher", None)
        if hook_matcher is None:
            return

        async def note_tool_use_id(
            payload: object, tool_use_id: str | None, _context: object
        ) -> _JsonObject:
            if isinstance(payload, dict):
                name = str(payload.get("tool_name") or "")
                tool_use_id = tool_use_id or payload.get("tool_use_id")
                if name.startswith(_OMNIGENT_MCP_PREFIX) and isinstance(tool_use_id, str):
                    self._unclaimed_tool_use_ids[tool_use_id] = (
                        name.removeprefix(_OMNIGENT_MCP_PREFIX),
                        _args_key(payload.get("tool_input")),
                    )
            return {}

        hooks = dict(getattr(options, "hooks", None) or {})
        hooks.setdefault("PreToolUse", []).append(hook_matcher(hooks=[note_tool_use_id]))
        options.hooks = hooks

    def _claim_tool_use_id(self, tool_name: str, args: ToolArgs) -> str | None:
        """The oldest unclaimed ``tool_use_id`` noted for this exact call, if any."""
        key = (tool_name, _args_key(args))
        for tool_use_id, noted in self._unclaimed_tool_use_ids.items():
            if noted == key:
                del self._unclaimed_tool_use_ids[tool_use_id]
                return tool_use_id
        return None

    def _install_subagent_router_hook(
        self,
        sdk: _ClaudeSDK,
        options: Any,  # type: ignore[explicit-any]  # ClaudeAgentOptions — avoid a hard sdk import
        model: str | None,
    ) -> None:
        """
        Register the in-process subagent-routing ``PreToolUse`` hook.

        The claude-agent-sdk runs hook callbacks in this process, so the
        native hook script's decision logic is imported instead of
        subprocessed. No-op unless the runner advertises a
        ``route-subagent`` endpoint, so unrouted sessions register nothing.

        :param sdk: The ``claude_agent_sdk`` module (or a test double).
        :param options: ``ClaudeAgentOptions`` to mutate.
        :param model: Model this session runs on, sent as the spawn's
            parent model.
        """
        hook_matcher_cls = getattr(sdk, "HookMatcher", None)
        if hook_matcher_cls is None:
            return
        router_dir = subagent_router.discover_router_dir()
        if subagent_router.read_router_endpoint(router_dir) is None:
            return

        async def route_spawn(
            payload: Any,  # type: ignore[explicit-any]  # HookInput TypedDict
            tool_use_id: str | None,  # noqa: ARG001 -- HookCallback signature
            context: Any,  # type: ignore[explicit-any]  # HookContext  # noqa: ARG001 -- HookCallback signature
        ) -> dict[str, Any]:  # type: ignore[explicit-any]  # HookJSONOutput
            if not isinstance(payload, dict):
                return {}
            output = await asyncio.to_thread(
                subagent_router.route_pre_tool_use,
                payload,
                harness="claude-sdk",
                router_dir=router_dir,
                parent_model=model,
            )
            return output or {}

        hooks = dict(getattr(options, "hooks", None) or {})
        entries = list(hooks.get("PreToolUse") or [])
        entries.append(
            hook_matcher_cls(
                matcher=subagent_router.AGENT_TOOL_MATCHER,
                # Strictly outside the router call's own HTTP budget: equal
                # numbers let the SDK cancel the hook at the same instant its
                # request gives up, so the fail-open branch never ran.
                timeout=subagent_router.HOOK_TIMEOUT_S,
                hooks=[route_spawn],
            )
        )
        hooks["PreToolUse"] = entries
        options.hooks = hooks

    async def _can_use_tool_for_permission(
        self,
        tool_name: str,
        tool_input: ToolArgs,
        perm_ctx: object,
    ) -> object:
        """
        Route a Claude SDK permission request through the Omnigent elicitation system.

        Installed as ``options.can_use_tool`` when ``permission_mode`` is
        not ``"bypassPermissions"`` and an elicitation handler has been wired
        in by :class:`omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter`.
        Called by the SDK before each tool invocation.

        :param tool_name: Name of the tool Claude wants to call,
            e.g. ``"Bash"``.
        :param tool_input: Arguments dict for the tool call,
            e.g. ``{"command": "ls -la"}``.
        :param perm_ctx: :class:`claude_agent_sdk.ToolPermissionContext`
            carrying ``tool_use_id`` (the SDK-assigned id for this specific
            invocation), ``agent_id`` (non-None inside sub-agents), and
            ``suggestions`` (permission hints from the CLI). Used here for
            diagnostic logging so unexpected permission requests are traceable.
        :returns: :class:`claude_agent_sdk.PermissionResultAllow` when the
            user approves, or :class:`claude_agent_sdk.PermissionResultDeny`
            with a ``message`` when they deny.
        """
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        tool_use_id: str | None = getattr(perm_ctx, "tool_use_id", None)
        logger.debug(
            "permission request: tool=%s tool_use_id=%s",
            tool_name,
            tool_use_id,
        )
        if self._elicitation_handler is None:
            # No handler installed — grant by default. This branch is
            # unreachable in normal operation (run_turn only sets
            # can_use_tool when _elicitation_handler is not None), but
            # is included for safety if the SDK ever calls the callback
            # after the handler was cleared.
            return PermissionResultAllow()
        allowed = await self._elicitation_handler(tool_name, tool_input)
        if allowed:
            return PermissionResultAllow()
        return PermissionResultDeny(message="Denied via Omnigent elicitation")

    async def _evaluate_tool_call_policy(
        self,
        tool_name: str,
        tool_input: ToolArgs,
    ) -> object | None:
        """
        Run a pre-execution TOOL_CALL policy evaluation for one tool call.

        This is the policy half of the ``can_use_tool`` gate. It exists
        so connector-native MCP tools — ones injected by the Claude Agent
        SDK / claude.ai connector layer (e.g. ``mcp__github__*``,
        ``mcp__atlassian__*``) that are NOT part of the agent spec's
        ``mcp_servers`` and execute INSIDE the CLI subprocess — get
        evaluated against Omnigent TOOL_CALL-phase policies BEFORE they
        run. Without this gate those calls bypass policy entirely (the
        executor only OBSERVES them in the message stream, which posts no
        policy event).

        Double-evaluation guard: Omnigent's OWN tools are exposed as the
        single ``omnigent`` SDK MCP server (the model sees
        ``mcp__omnigent__*``). When the model calls one, the SDK wrapper
        routes it back through Omnigent's dispatch bridge
        (``_stable_tool_executor`` -> ``TurnContext.dispatch_tool`` ->
        ``action_required``), and the runner re-dispatches it via
        ``ProxyMcpManager``, which enforces TOOL_CALL + TOOL_RESULT
        policies server-side before forwarding to ``/mcp/execute``
        (see ``omnigent/runner/app.py`` "All tool calls go through AP:/mcp
        ... which enforces TOOL_CALL + TOOL_RESULT policies server-side").
        Spec-declared MCP tools are surfaced through that same
        ``mcp__omnigent__*`` server, so they are covered there too.
        Evaluating ``mcp__omnigent__*`` here as well would double-count
        the same call (and could double-charge a cost-budget checkpoint),
        so we SKIP that prefix and only gate the connector-native /
        out-of-band tools the dispatch path never sees.

        :param tool_name: Full SDK tool name, e.g.
            ``"mcp__github__issue_write"``.
        :param tool_input: Arguments dict for the tool call.
        :returns: :class:`claude_agent_sdk.PermissionResultDeny` when a
            policy denies the call. ``POLICY_ACTION_ASK`` is normally
            collapsed to a hard ALLOW/DENY by the server-side
            ``/policies/evaluate`` route. If a raw ASK reaches this callback
            (for example from a read-only evaluation path), this hook runs the
            existing Omnigent elicitation handler before returning
            :class:`claude_agent_sdk.PermissionResultAllow` or DENY; without
            a handler it fails closed. Returns ``None`` when the call should
            be allowed to proceed (no policy evaluator wired, an
            ``mcp__omnigent__*`` tool already gated on the dispatch path, or
            an ALLOW / no-match verdict). Returning ``None`` lets the caller
            fall through to its remaining gate logic (elicitation) without
            forcing an allow.
        """
        _policy_eval = getattr(self, "_policy_evaluator", None)
        if _policy_eval is None:
            return None
        # Omnigent's own tools are already TOOL_CALL-gated server-side via
        # the dispatch bridge / ProxyMcpManager — don't evaluate them twice.
        if tool_name.startswith("mcp__omnigent__"):
            return None
        _verdict = await _policy_eval(
            "PHASE_TOOL_CALL",
            {"name": tool_name, "arguments": tool_input},
        )
        _action = getattr(_verdict, "action", None)
        if _action in ("POLICY_ACTION_ALLOW", "POLICY_ACTION_UNSPECIFIED"):
            # ALLOW / no-match — fall through (caller decides whether to also
            # run the human-consent elicitation gate).
            return None
        if _action == "POLICY_ACTION_ASK":
            from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

            reason = _verdict.reason or "Approval required by Omnigent TOOL_CALL policy"
            if self._elicitation_handler is None:
                logger.warning(
                    "TOOL_CALL policy ASK had no elicitation handler; denying tool=%s reason=%s",
                    tool_name,
                    reason,
                )
                return PermissionResultDeny(message=reason)
            logger.info("TOOL_CALL policy requested approval tool=%s reason=%s", tool_name, reason)
            if await self._elicitation_handler(tool_name, tool_input):
                return PermissionResultAllow()
            return PermissionResultDeny(message=reason)
        if _action == "POLICY_ACTION_DENY":
            from claude_agent_sdk import PermissionResultDeny

            reason = _verdict.reason or "Denied by Omnigent TOOL_CALL policy"
            logger.info("TOOL_CALL policy denied tool=%s reason=%s", tool_name, reason)
            return PermissionResultDeny(message=reason)
        from claude_agent_sdk import PermissionResultDeny

        reason = f"Unexpected Omnigent TOOL_CALL policy verdict: {_action!r}"
        logger.warning("TOOL_CALL policy failed closed tool=%s reason=%s", tool_name, reason)
        return PermissionResultDeny(message=reason)

    async def _can_use_tool_gate(
        self,
        tool_name: str,
        tool_input: ToolArgs,
        perm_ctx: object,
    ) -> object:
        """
        Unified ``options.can_use_tool`` callback for the claude-sdk path.

        Composes two independent gates, in order:

        1. **TOOL_CALL policy** (always, when a ``_policy_evaluator`` is
           wired): a hard DENY short-circuits to
           :class:`~claude_agent_sdk.PermissionResultDeny`. This runs in
           EVERY permission mode — including ``bypassPermissions`` — so
           connector-native MCP tools can't slip past policy. ALLOW /
           no-match falls through with no human interaction, preserving
           ``bypassPermissions`` ergonomics for un-gated tools.
        2. **Human-consent elicitation** (only when the permission mode is
           NOT ``bypassPermissions`` and an elicitation handler is wired):
           the pre-existing per-tool approval prompt. Under
           ``bypassPermissions`` this step is skipped entirely, so the
           model still acts autonomously for anything policy allows.

        :param tool_name: Full SDK tool name, e.g. ``"Bash"`` or
            ``"mcp__github__issue_write"``.
        :param tool_input: Arguments dict for the tool call.
        :param perm_ctx: :class:`claude_agent_sdk.ToolPermissionContext`.
        :returns: A :class:`~claude_agent_sdk.PermissionResult`.
        """
        from claude_agent_sdk import PermissionResultAllow

        policy_result = await self._evaluate_tool_call_policy(tool_name, tool_input)
        if policy_result is not None:
            # Hard DENY from policy — block before execution.
            return policy_result
        # Policy allowed (or no policy gate). Under bypassPermissions we
        # never prompt; otherwise defer to the elicitation gate.
        if self._permission_mode == "bypassPermissions" or self._elicitation_handler is None:
            return PermissionResultAllow()
        return await self._can_use_tool_for_permission(tool_name, tool_input, perm_ctx)

    def _bundle_skill_names(self) -> list[str]:
        """Plugin-qualified names of the bundle's own skills (may be empty)."""
        if self._bundle_dir is None:
            return []
        root = pathlib.Path(self._bundle_dir) / "skills"
        if not root.is_dir():
            return []
        return sorted(
            f"{self._agent_name}:{d.name}" for d in root.iterdir() if (d / "SKILL.md").is_file()
        )

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """Run one turn via the Claude Agent SDK.

        The SDK receives the latest user message as a prompt and runs its full
        agent loop (which may include multiple internal tool calls).  We
        observe the message stream and yield ExecutorEvents for the Session
        to record in History.
        """
        sdk = cast(_ClaudeSDK, _ensure_sdk())
        from claude_agent_sdk.types import StreamEvent as _StreamEvent

        cfg = config or ExecutorConfig()

        session_key = self._session_key(messages)
        self._interrupted_sessions.discard(session_key)
        crashed_reason = self._crashed_sessions.get(session_key)
        if crashed_reason is not None:
            yield ExecutorError(
                message=(
                    "Claude SDK session crashed and cannot continue in this Session. "
                    f"Start a new Session. Cause: {crashed_reason}"
                )
            )
            return
        warm_state = self._clients.get(session_key)
        # A pre-launched client has seen no turn: it gets the whole history.
        resume_session = warm_state is not None and getattr(warm_state, "primed", True)
        prompt = self._build_prompt(
            messages,
            resume_session=resume_session,
        )
        # An empty first message on a session with no CLI yet is the runner launching it
        # ahead of the first turn (ADR 0026: cold start is paid before the person speaks).
        prelaunch = not prompt and warm_state is None and bool(system_prompt)
        if not prompt and not prelaunch:
            # Resumed sessions can have nothing new to say; signal turn
            # completion with no assistant text instead of an empty string.
            yield TurnComplete(response=None)
            return

        # Build MCP tools from Omnigent tool schemas
        mcp_tools = (
            _build_mcp_tools(tools, self._tool_executor, self._claim_tool_use_id) if tools else []
        )

        # Create MCP server config for Omnigent tools. The SDK's
        # ``McpServerConfig`` union is opaque to us — we pass through
        # whatever ``create_sdk_mcp_server`` returns.
        mcp_servers: dict[str, Any] = {}  # type: ignore[explicit-any]
        if mcp_tools:
            mcp_servers["omnigent"] = sdk.create_sdk_mcp_server(
                name="omnigent",
                version="1.0.0",
                tools=mcp_tools,
            )
            system_prompt = _augment_system_prompt_for_omnigent_mcp_tools(
                system_prompt,
                tools,
            )

        # Build allowed_tools list.  OS-environment operations route
        # through Omnigent ``sys_os_*`` MCP tools rather than the
        # SDK's native Bash/Read/Edit/Write — MCP tools flow through
        # the scaffold's ``dispatch_tool`` path, giving the runner
        # visibility, timeouts, and error recovery.
        #
        # In ``auto`` and ``bypassPermissions`` modes, pre-approve all
        # MCP tools so the agent can act autonomously without a per-call
        # human-consent gate.  ``auto`` still runs background safety
        # checks; ``bypassPermissions`` skips all gates entirely.
        # In any other mode (``default``, ``acceptEdits``, etc.), leave
        # ``allowed_tools`` empty so every tool call goes through the
        # SDK's ``can_use_tool`` callback — which routes to the AP
        # elicitation system when an elicitation handler is wired in.
        # When ``allowed_tools`` is empty the SDK omits ``--allowedTools``
        # entirely, letting Claude's normal permission flow apply.
        allowed_tools: list[str] = []
        if self._permission_mode in ("auto", "bypassPermissions"):
            # Allow all Omnigent MCP tools (no per-call human gate needed)
            for schema in tools:
                raw_tname = schema.get("name")
                # Claude SDK's ``allowed_tools`` requires concrete strings;
                # Omnigent tool schemas always carry a name (see
                # ``Tool.tool_schema``), but defend against malformed specs
                # by skipping unnamed entries rather than producing a
                # bogus ``mcp__omnigent__`` allow-entry.
                if not isinstance(raw_tname, str) or not raw_tname:
                    continue
                allowed_tools.append(f"mcp__omnigent__{raw_tname}")

        # cfg.model > spec model > None (lets the SDK pick its own default).
        # The Omnigent producer resolves a concrete model (spec > provider
        # default > catalog default) before spawning on the gateway path.
        model = cfg.model or self._model_override

        # Omnigent owns context, memory and sub-agents for a superside-chat
        # session (docs/adr/0006-omnigent-owns-everything.md); every
        # competing Claude Code feature below is gated on this and a no-op
        # otherwise.
        is_superside_chat = cfg.extra.get("context_mode") == SUPERSIDE_CHAT_MODE_VALUE

        # Build env: gateway settings resolved at construction. CLAUDECODE
        # removal happens around the subprocess spawn in
        # ``_get_or_create_client`` via ``_unset_env_var`` — setting it to
        # ``""`` here would still leave an empty key in the child env.
        env = dict(self._extra_env)
        if prompt_cache_1h():
            # A 1-hour prompt-cache TTL instead of 5 minutes (ADR 0031 step 0b).
            env["ENABLE_PROMPT_CACHING_1H"] = "1"
        if is_superside_chat:
            env[_CLAUDE_CODE_DISABLE_AUTO_COMPACT_ENV] = "1"
            env[_CLAUDE_CODE_DISABLE_AUTO_MEMORY_ENV] = "1"
            # Its Omnigent tools are few and every turn needs some: load their definitions
            # with the session instead of through a ToolSearch call before the first one.
            env[_CLAUDE_CODE_ENABLE_TOOL_SEARCH_ENV] = "false"
        api_key_helper = env.pop(_CLAUDE_API_KEY_HELPER_ENV_KEY, None)
        # Teach Claude Code this gateway's spellings so no model surface routes
        # to an id the gateway rejects: pins for the family aliases, rewrites
        # for the canonical ids the CLI names itself (the refusal-fallback).
        # No-op off the gateway transport.
        model_overrides = await self._apply_gateway_model_vocabulary(env, api_key_helper)
        settings_payload = _claude_settings_payload(api_key_helper, model_overrides)
        # Dev only (OMNIGENT_LLM_TRACE_DIR): every model call goes through the logging proxy.
        from omnigent.harnesses import llm_trace

        traced = llm_trace.base_url_for(
            session_key,
            env.get("ANTHROPIC_BASE_URL")
            or os.environ.get("ANTHROPIC_BASE_URL")
            or "https://api.anthropic.com",
        )
        if traced is not None:
            env["ANTHROPIC_BASE_URL"] = traced

        # Capture stderr from the CLI subprocess for diagnostics
        stderr_lines: list[str] = []

        def _on_stderr(line: str) -> None:
            stderr_lines.append(line)
            logger.debug("Claude CLI stderr: %s", line)

        # Build options.
        #
        # ``skills="all"`` makes the Claude Agent SDK auto-configure
        # the ``Skill`` tool in ``allowed_tools`` and default
        # ``setting_sources`` to ``["user", "project"]`` so the CLI
        # discovers user-installed skills under ``~/.claude/skills/``
        # and project-local skills under ``<cwd>/.claude/skills/``.
        # See ``claude_agent_sdk._internal.transport.subprocess_cli.
        # _apply_skills_defaults`` for the auto-derivation.
        #
        # ``tools`` is the model's BASE tool set; ``allowed_tools``
        # is just a permission filter on it. ``skills="all"`` only
        # adds ``Skill`` to ``allowed_tools`` — to actually expose
        # the tool to the model we have to put it in ``tools`` too.
        # Without this, the SDK passes ``--tools ""`` to the CLI
        # which ZEROS the base set, and the agent never sees a
        # ``Skill`` tool even with ``--allowedTools=Skill`` set
        # (the live regression that makes the agent answer "I
        # don't have a Skill tool exposed in this session").
        #
        # ``--bare`` (formerly in ``extra_args``) is intentionally
        # NOT passed: bare mode skips CLAUDE.md auto-discovery,
        # plugin sync, and auto-memory — exactly the host config
        # users expect to leak through to a ``claude-sdk`` harness
        # they explicitly opted into. ``no-session-persistence``
        # stays because omnigent owns conversation persistence
        # via its own conversation store.
        # OS-environment tools are provided via Omnigent ``sys_os_*``
        # MCP tools (declared via ``os_env`` in the spec), not the
        # SDK's native Bash/Read/Edit/Write. Keep Skill and ToolSearch in
        # the base set so MCP definitions can be discovered on demand.
        base_tools: list[str] = ["Skill", "ToolSearch"] if self._native_skills else ["ToolSearch"]
        # Translate the spec's host-skill filter into the SDK
        # options. Falls back to ``"all"`` semantics when the
        # field is malformed (the parser already validates, so
        # this is belt-and-suspenders).
        resolved = _resolve_skills_option(self._skills_filter) or _ResolvedSkills(
            skills="all", setting_sources=None
        )
        # Bundle skills are exposed via the SDK's plugin mechanism.
        # The bundle's ``<bundle>/skills/<dir>/SKILL.md`` files are
        # discovered as plugin skills (no ``.claude/`` prefix needed
        # under the plugin convention — see plugin discovery test in
        # tests/harnesses/test_claude_sdk_executor.py). The plugin's
        # ``name`` (and thus the skill-listing prefix) comes from
        # the manifest written at construction time.
        bundle_plugins: list[Any] = []  # type: ignore[explicit-any]  # SdkPluginConfig is a TypedDict — typed Any here to keep the import lazy
        if self._bundle_dir is not None and self._native_skills:
            bundle_plugins.append({"type": "local", "path": str(self._bundle_dir)})
        options_kwargs: dict[str, Any] = {  # type: ignore[explicit-any]  # ClaudeAgentOptions accepts mixed-typed kwargs (str / list / dict / callable / etc.)
            "tools": base_tools,
            "system_prompt": system_prompt or None,
            "mcp_servers": mcp_servers if mcp_servers else {},
            "allowed_tools": allowed_tools,
            "permission_mode": self._permission_mode,
            "max_turns": cfg.extra.get("max_turns"),
            "env": env,
            "settings": settings_payload,
            "stderr": _on_stderr,
            "include_partial_messages": True,
            "include_hook_events": True,
            "skills": resolved.skills,
            "plugins": bundle_plugins,
            # ``replay-user-messages`` makes the CLI echo each user message it takes
            # up (with the uuid we sent), which is how a mid-turn steer is seen to
            # have been consumed -- see ``steer_session_message``.
            # A named session skips the CLI's own title call, a model call over the
            # whole first prompt that nothing here reads.
            "extra_args": {
                "no-session-persistence": None,
                "replay-user-messages": None,
                **({} if cfg.session_titles else {"name": "omnigent"}),
            },
            "max_buffer_size": 10 * 1024 * 1024,
            "disallowed_tools": [
                *_CLI_TASK_TOOLS,
                *(_SDK_BUILTIN_SUBAGENT_TOOLS if is_superside_chat else ()),
                # The CLI offers Skill even when ``tools`` leaves it out, like the Task tools.
                *(() if self._native_skills else ("Skill",)),
            ],
        }
        # Only forward ``setting_sources`` when explicitly set.
        # ``None`` lets the SDK apply its default
        # (``["user", "project"]`` when ``skills`` is non-None).
        # An empty list — produced by ``"none"`` — is forwarded
        # verbatim so the SDK doesn't auto-default it back to
        # ``["user", "project"]``, which would re-load host
        # skills into the model's system prompt despite
        # ``skills=[]`` (the live regression that prompted this
        # branch).
        if resolved.setting_sources is not None:
            options_kwargs["setting_sources"] = resolved.setting_sources
        if not self._native_skills:
            # Omnigent owns this bundle's skills (``load_skill``, ADR 0006): no native listing.
            options_kwargs["skills"] = []
            options_kwargs["setting_sources"] = []
        if is_superside_chat:
            # No CLAUDE.md / filesystem settings: Omnigent owns every
            # instruction this session gets (system prompt + framework
            # instructions below), never the CLI's own file discovery.
            options_kwargs["setting_sources"] = []
            # The CLI's own bundled skills (dataviz, code-review, ...) are
            # listed regardless of setting_sources. Allow-list only this
            # bundle's skills; ``[]`` hides everything else from the model.
            options_kwargs["skills"] = self._bundle_skill_names()
        try:
            reasoning_effort = validate_effort(
                cfg.extra.get("reasoning_effort"), "Claude Agent SDK", CLAUDE_EFFORTS
            )
        except ValueError as exc:
            yield ExecutorError(message=describe_exception(exc), retryable=False)
            return
        if reasoning_effort is not None:
            options_kwargs["effort"] = reasoning_effort
        options = sdk.ClaudeAgentOptions(**options_kwargs)
        if self._cli_path:
            options.cli_path = self._cli_path
        if model:
            options.model = model
        if self._cwd:
            options.cwd = self._cwd
        # Install the unified can_use_tool gate. It runs the TOOL_CALL
        # policy evaluation in EVERY permission mode — including
        # ``bypassPermissions`` — so connector-native MCP tools
        # (``mcp__github__*``, ``mcp__atlassian__*``) that execute inside
        # the CLI subprocess are evaluated against Omnigent TOOL_CALL
        # policy before they run, instead of bypassing policy entirely.
        #
        # The gate is no-friction when nothing matches: a policy ALLOW /
        # no-match returns allow with no human prompt, so
        # ``bypassPermissions`` ergonomics are preserved for un-gated
        # tools. The human-consent elicitation half of the gate still only
        # fires for non-bypass modes (see ``_can_use_tool_gate``).
        #
        # Install whenever EITHER a policy evaluator OR an elicitation
        # handler is wired. Previously this only installed for non-bypass
        # modes, which is why default ``claude-sdk`` sessions (which
        # default to ``bypassPermissions``) had no per-tool TOOL_CALL gate.
        if (
            getattr(self, "_policy_evaluator", None) is not None
            or self._elicitation_handler is not None
        ):
            options.can_use_tool = self._can_use_tool_gate

        self._install_subagent_router_hook(sdk, options, model)
        self._install_framework_context_hook(sdk, options, session_key)
        self._install_ends_turn_hook(sdk, options)
        self._install_tool_use_id_hook(sdk, options)

        # Log the full configuration for debugging
        logger.info(
            "ClaudeSDKExecutor: model=%s, gateway=%s, base_url=%s, tools=%d, thinking=%r",
            model or "(default)",
            self._gateway,
            env.get("ANTHROPIC_BASE_URL", "(not set)"),
            len(tools),
            options_kwargs.get("thinking"),
        )
        launch_key = _launch_key(system_prompt, tools, reasoning_effort, cfg)
        if prelaunch:
            try:
                await self._get_or_create_client(
                    sdk,
                    session_key=session_key,
                    options=options,
                    model=model,
                    rebuild_on_instructions_change=is_superside_chat,
                    launch_key=launch_key,
                    primed=False,
                )
            except Exception:
                # The first turn launches it as before.
                logger.warning("Claude CLI pre-launch failed", exc_info=True)
            yield TurnComplete(response=None)
            return

        # Run the query, streaming events as they arrive.
        # The SDK manages its own tool-call loop internally (via MCP).
        # We observe the message stream and yield ExecutorEvents so the
        # CLI can render text chunks and tool-call progress in real time.
        response_text = ""
        separate_next_text = False
        turn_usage: dict[str, Any] | None = None  # type: ignore[explicit-any]
        # Billing usage summed over the turn's ``ResultMessage``s (one per CLI turn).
        billed_usage: dict[str, Any] | None = None  # type: ignore[explicit-any]
        # The concrete model the SDK reports on its assistant messages, e.g.
        # ``"claude-opus-4-8"``. Captured from the stream because the resolved
        # config ``model`` is ``None`` when the spec pins none and the gateway
        # picks a default internally — this is then forwarded in ``turn_usage``
        # so the server can price the turn. ``None`` until an assistant message
        # carrying a model arrives.
        observed_model: str | None = None
        system_diagnostics: list[str] = []
        terminal_error: str | None = None
        # Public failure code for ``terminal_error`` (``None`` -> the adapter says ``internal``).
        terminal_code: str | None = None
        # Provider failure the CLI reported as an assistant message: its error marker, and its
        # text, which is withheld from the transcript as assistant content.
        provider_error_type: str | None = None
        provider_error_text = ""
        compaction_occurred: bool = False
        claude_session_id: str | None = None
        compaction_transcript_path: pathlib.Path | None = None
        compaction_transcript_offset: int | None = None

        # Track in-flight tool calls so we can emit ToolCallComplete
        # with the tool name and duration when results arrive.
        pending_tools: dict[str, tuple[str, float]] = {}  # id → (name, start_mono)
        # (parent call, stream block index) → (tool id, name), so ``input_json_delta`` (keyed
        # by index within its own stream) can name its call.
        streaming_tools: dict[object, tuple[str, str]] = {}

        # Track whether we've received any StreamEvent messages.
        # When True, we skip text/tool events from AssistantMessage to
        # avoid double-emitting (the SDK sends both StreamEvents AND
        # the complete AssistantMessage for the same content).
        got_stream_events = False

        # Per-API-call prompt usage from the most recent ``message_start``
        # stream event. A single user turn can drive MANY internal API
        # calls (the SDK's tool loop), and ``ResultMessage.usage`` reports
        # the CUMULATIVE total across all of them — so summing its cache
        # buckets over-counts context, because each iteration re-sends the
        # whole (growing) conversation and those prompt tokens get tallied
        # once per call. For context-window fill we want only the LAST
        # call's prompt size: that single prompt already contains the full
        # conversation that carries into the next turn. We capture each
        # ``message_start``'s ``message.usage`` here and keep the latest,
        # as the ``context_tokens`` source. ``None`` until the first call starts.
        last_call_usage: dict[str, Any] | None = None  # type: ignore[explicit-any]

        def _new_compact_summary_visible() -> bool:
            if compaction_transcript_path is None or compaction_transcript_offset is None:
                logger.warning(
                    "Skipping Claude compaction checkpoint after stream failure: "
                    "PreCompact did not provide a readable transcript boundary "
                    "(session=%s).",
                    claude_session_id,
                )
                return False

            try:
                with compaction_transcript_path.open("rb") as transcript:
                    transcript.seek(compaction_transcript_offset)
                    appended = transcript.read()
            except OSError:
                logger.warning(
                    "Skipping Claude compaction checkpoint after stream failure: "
                    "could not read appended transcript records (session=%s, path=%s).",
                    claude_session_id,
                    compaction_transcript_path,
                    exc_info=True,
                )
                return False

            for raw_line in appended.splitlines():
                try:
                    entry = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(entry, dict) and entry.get("isCompactSummary") is True:
                    return True

            logger.warning(
                "Skipping Claude compaction checkpoint after stream failure: "
                "no post-PreCompact summary record is visible yet (session=%s, path=%s).",
                claude_session_id,
                compaction_transcript_path,
            )
            return False

        def _build_compaction_complete_event(
            *, require_new_compact_summary: bool = False
        ) -> ExecutorEvent | None:
            from omnigent.harnesses.executor import CompactionComplete

            assert claude_session_id is not None
            if require_new_compact_summary and not _new_compact_summary_visible():
                return None

            compaction_tokens = 0
            if turn_usage is not None:
                compaction_tokens = turn_usage.get("context_tokens", 0) or 0

            try:
                from claude_agent_sdk import get_session_messages

                messages = get_session_messages(claude_session_id, directory=self._cwd)
                compacted_messages = [
                    {
                        "type": "message",
                        "role": message.type,
                        "content": message.message.get("content", []),
                    }
                    for message in messages
                    if isinstance(message.message, dict)
                ]
            except Exception:
                logger.warning(
                    "Failed to read Claude post-compaction session messages "
                    "(session=%s); preserving full server history instead of "
                    "persisting an empty checkpoint.",
                    claude_session_id,
                    exc_info=True,
                )
                return None

            if not compacted_messages:
                logger.warning(
                    "Claude post-compaction read returned no messages "
                    "(session=%s); preserving full server history instead of "
                    "persisting an empty checkpoint.",
                    claude_session_id,
                )
                return None

            return CompactionComplete(
                summary="[Claude Code compaction — context was automatically compacted]",
                token_count=compaction_tokens,
                model=observed_model or model,
                compacted_messages=compacted_messages,
            )

        client = await self._get_or_create_client(
            sdk,
            session_key=session_key,
            options=options,
            model=model,
            rebuild_on_instructions_change=is_superside_chat,
            launch_key=launch_key,
        )
        if (live_state := self._clients.get(session_key)) is not None:
            live_state.primed = True
        if resume_session and self._clients.get(session_key) is not warm_state:
            # The warm client was rebuilt (instructions changed): it is a cold
            # start and needs the full history, not only the trailing message.
            resume_session = False
            if prompt != "/compact":
                prompt = self._build_prompt(messages, resume_session=False)

        # ── LLM_REQUEST policy evaluation ────────────────────────
        # If the executor adapter installed a ``_policy_evaluator``
        # callback, call it with the request data so the Omnigent server
        # can evaluate LLM_REQUEST policies before the LLM call.
        _policy_eval = getattr(self, "_policy_evaluator", None)
        if _policy_eval is not None:
            # Extract the user prompt text for PII scanning.
            _last_user_msg = ""
            if isinstance(prompt, str):
                _last_user_msg = prompt[:500]
            elif isinstance(prompt, list):
                _parts: list[str] = []
                for block in prompt:
                    text = block.get("text")
                    if block.get("type") in ("text", "input_text") and isinstance(text, str):
                        _parts.append(text)
                _last_user_msg = " ".join(_parts)[:500]
            _req_data: _JsonObject = {
                "model": model,
                "messages_count": len(prompt) if isinstance(prompt, list) else 1,
                "tools_count": len(tools),
                "system_prompt_preview": (system_prompt[:200] if system_prompt else ""),
                "last_user_message": _last_user_msg,
            }
            _req_verdict = await _policy_eval("PHASE_LLM_REQUEST", _req_data)
            if _req_verdict.action == "POLICY_ACTION_DENY":
                _deny_reason = _req_verdict.reason or "no reason given"
                yield ExecutorError(message=f"LLM call denied by policy: {_deny_reason}")
                return

        notice_messages = messages
        if resume_session:
            notice_messages = []
            for message in reversed(messages):
                if message.get("role") != "user":
                    break
                notice_messages.insert(0, message)
        self._pending_framework_context[session_key] = "\n\n".join(
            notice
            for message in notice_messages
            if message.get("role") == "user"
            for notice in framework_notices(message.get("content"))
        )
        steer_window: _SteerWindow | None = None
        try:
            try:
                sdk_prompt: str | AsyncIterator[_JsonObject]
                if isinstance(prompt, list):
                    # Multimodal content blocks — send as a
                    # structured message via the SDK's dict path.
                    sdk_prompt = _multimodal_message_iter(prompt, session_id=session_key)
                else:
                    sdk_prompt = prompt
                from omnigent.harnesses import llm_trace

                llm_trace.event(session_key, "prompt", prompt)
                await asyncio.wait_for(
                    client.query(sdk_prompt, session_id=session_key),
                    timeout=_QUERY_START_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    f"Claude SDK query start timed out after {int(_QUERY_START_TIMEOUT_SECONDS)}s"
                ) from exc
            # Only now may a steer be pushed: the prompt is on the CLI's stdin ahead of it.
            steer_window = _SteerWindow()
            self._steer_windows[session_key] = steer_window
            message_stream = self._receive_with_steers(sdk, client, steer_window)
            try:
                while True:
                    next_task = asyncio.ensure_future(anext(message_stream))
                    idle_seconds = 0.0
                    try:
                        while True:
                            done, _ = await asyncio.wait(
                                {next_task}, timeout=_STREAM_IDLE_WARN_SECONDS
                            )
                            if next_task in done:
                                break
                            idle_seconds += _STREAM_IDLE_WARN_SECONDS
                            logger.warning(
                                "Claude SDK response stream has been idle for "
                                "%ds (session %s); still waiting.",
                                int(idle_seconds),
                                session_key,
                            )
                    except BaseException:
                        next_task.cancel()
                        with suppress(BaseException):
                            await next_task
                        raise
                    try:
                        message = next_task.result()
                    except StopAsyncIteration:
                        break
                    if isinstance(message, SteerOutcome):
                        yield message
                        continue
                    if isinstance(message, _StreamEvent):
                        got_stream_events = True
                        stream_evt = cast(_StreamEventObj, message)
                        evt = stream_evt.event
                        evt_type = evt.get("type")

                        if evt_type == "message_start":
                            # Each ``message_start`` opens one API call and
                            # carries that call's prompt-side usage
                            # (input + cache buckets). Keep the latest so
                            # ``context_tokens`` reflects the final call's
                            # prompt — the true context size — instead of
                            # ``ResultMessage.usage``'s cumulative sum across
                            # every tool-loop iteration.
                            msg_obj = evt.get("message")
                            if isinstance(msg_obj, dict):
                                call_usage = msg_obj.get("usage")
                                if isinstance(call_usage, dict) and call_usage:
                                    last_call_usage = call_usage
                                    yield ContextFill(
                                        tokens=sum(
                                            int(call_usage.get(key) or 0)
                                            for key in (
                                                "input_tokens",
                                                "cache_creation_input_tokens",
                                                "cache_read_input_tokens",
                                            )
                                        )
                                    )
                            continue

                        if evt_type == "content_block_start":
                            block_evt = evt.get("content_block", {})
                            block_type = block_evt.get("type")
                            if block_type == "thinking":
                                # Anchor the ``thinking…`` indicator at the
                                # start of an extended-thinking block.
                                yield ReasoningChunk(
                                    delta="",
                                    event_type="reasoning_started",
                                )
                                continue
                            if block_type == "text" and response_text.strip():
                                # A new text block after earlier text: mark the
                                # boundary so consumers never see glued blocks.
                                separate_next_text = True
                            if block_type == "tool_use":
                                raw_tool_id = block_evt.get("id")
                                # SSE stream events from the SDK should
                                # always carry an ``id`` on tool_use
                                # blocks; skip malformed events rather
                                # than bucketing them under ``""``.
                                if not isinstance(raw_tool_id, str):
                                    continue
                                tool_name = block_evt.get("name", "unknown")
                                # Track the tool for duration / pairing.
                                # The ToolCallRequest itself is emitted
                                # later, when the ``AssistantMessage``
                                # arrives with ``tool_block.input``
                                # populated — at ``content_block_start``
                                # the args have not yet been streamed via
                                # ``input_json_delta`` and the request
                                # would carry ``args={}``, rendering
                                # downstream as ``⏵ Bash()`` with no
                                # visible command. Waiting one event
                                # loses a few ms of "tool call started"
                                # feedback in exchange for correct args.
                                pending_tools[raw_tool_id] = (tool_name, time.monotonic())
                                streaming_tools[
                                    (
                                        getattr(message, "parent_tool_use_id", None),
                                        evt.get("index"),
                                    )
                                ] = (raw_tool_id, tool_name)
                                yield ToolCallStarted(
                                    name=tool_name, metadata=_call_metadata(raw_tool_id, message)
                                )

                        elif evt_type == "content_block_delta":
                            delta = evt.get("delta", {})
                            delta_type = delta.get("type")
                            if delta_type == "text_delta":
                                text = delta.get("text")
                                if isinstance(text, str) and text:
                                    if separate_next_text:
                                        separate_next_text = False
                                        response_text += _TEXT_BLOCK_SEPARATOR
                                        yield TextChunk(text=_TEXT_BLOCK_SEPARATOR)
                                    response_text += text
                                    yield TextChunk(text=text)
                            elif delta_type == "thinking_delta":
                                # Mirror the OpenAI reasoning path so the
                                # ``thinking…`` panel populates live.
                                thinking_text = delta.get("thinking")
                                if isinstance(thinking_text, str) and thinking_text:
                                    yield ReasoningChunk(
                                        delta=thinking_text,
                                        event_type="reasoning_text",
                                    )
                            elif delta_type == "input_json_delta":
                                streaming = streaming_tools.get(
                                    (
                                        getattr(message, "parent_tool_use_id", None),
                                        evt.get("index"),
                                    )
                                )
                                partial = delta.get("partial_json")
                                if streaming is not None and isinstance(partial, str) and partial:
                                    yield ToolInputChunk(
                                        name=streaming[1],
                                        delta=partial,
                                        metadata={"call_id": streaming[0]},
                                    )

                    elif isinstance(message, sdk.AssistantMessage):
                        assistant_msg = cast(_AssistantMessageObj, message)
                        # A provider failure (no credit, rate limit, bad key) arrives from
                        # the CLI as an assistant message carrying an error marker. It is a
                        # failed turn, not something the assistant said.
                        _am_error = getattr(assistant_msg, "error", None)
                        if isinstance(_am_error, str) and _am_error:
                            provider_error_type = _am_error
                        # Capture the concrete model the SDK used (the resolved
                        # config ``model`` is None when the spec pins none).
                        _am_model = concrete_reported_model(getattr(assistant_msg, "model", None))
                        if _am_model is not None:
                            observed_model = _am_model
                        if got_stream_events:
                            # StreamEvents already emitted text. Emit the
                            # ToolCallRequest here, once the full
                            # ``tool_block.input`` has assembled — see the
                            # note in the ``content_block_start`` branch
                            # above for why this is deferred.
                            for block in assistant_msg.content:
                                if isinstance(block, sdk.ToolUseBlock):
                                    tool_block = cast(_ToolUseBlockObj, block)
                                    if tool_block.id not in pending_tools:
                                        pending_tools[tool_block.id] = (
                                            tool_block.name,
                                            time.monotonic(),
                                        )
                                    yield ToolCallRequest(
                                        name=tool_block.name,
                                        args=tool_block.input,
                                        # Thread the SDK's
                                        # ``tool_use_id`` through
                                        # so downstream consumers
                                        # (notably the new harness
                                        # contract's
                                        # :class:`ExecutorAdapter`)
                                        # can pair this request
                                        # with the matching
                                        # :class:`ToolCallComplete`
                                        # by call_id.
                                        metadata=_call_metadata(tool_block.id, message),
                                    )
                        else:
                            # No streaming — emit events from the full message.
                            for block in assistant_msg.content:
                                if isinstance(block, sdk.TextBlock):
                                    text_block = cast(_TextBlockObj, block)
                                    if provider_error_type:
                                        provider_error_text += text_block.text
                                        continue
                                    if response_text.strip() and text_block.text:
                                        response_text += _TEXT_BLOCK_SEPARATOR
                                        yield TextChunk(text=_TEXT_BLOCK_SEPARATOR)
                                    response_text += text_block.text
                                    yield TextChunk(text=text_block.text)
                                elif isinstance(block, sdk.ThinkingBlock):
                                    thinking_block = cast(_ThinkingBlockObj, block)
                                    # Non-streaming counterpart of the
                                    # ``thinking_delta`` path above.
                                    if thinking_block.thinking:
                                        yield ReasoningChunk(
                                            delta="",
                                            event_type="reasoning_started",
                                        )
                                        yield ReasoningChunk(
                                            delta=thinking_block.thinking,
                                            event_type="reasoning_text",
                                        )
                                elif isinstance(block, sdk.ToolUseBlock):
                                    tool_block = cast(_ToolUseBlockObj, block)
                                    pending_tools[tool_block.id] = (
                                        tool_block.name,
                                        time.monotonic(),
                                    )
                                    yield ToolCallRequest(
                                        name=tool_block.name,
                                        args=tool_block.input,
                                        metadata=_call_metadata(tool_block.id, message),
                                    )

                    elif isinstance(message, sdk.UserMessage):
                        # Tool results come back as UserMessages containing
                        # ToolResultBlocks.  Match each back to its request.
                        user_msg = cast(_UserMessageObj, message)
                        content = user_msg.content
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, sdk.ToolResultBlock):
                                    result_block = cast(_ToolResultBlockObj, block)
                                    # A call that never reached its handler (denied,
                                    # unknown) leaves no id behind for a later lookalike.
                                    self._unclaimed_tool_use_ids.pop(
                                        result_block.tool_use_id, None
                                    )
                                    tool_name, start = pending_tools.pop(
                                        result_block.tool_use_id,
                                        ("unknown", time.monotonic()),
                                    )
                                    duration_ms = (time.monotonic() - start) * 1000
                                    classification = classify_tool_result(
                                        result_block.content,
                                        fallback_to_string=bool(result_block.is_error),
                                    )
                                    status = classification.status
                                    error = classification.error
                                    if result_block.is_error and not error:
                                        status = ToolCallStatus.ERROR
                                        error = "tool error"
                                    yield ToolCallComplete(
                                        name=tool_name,
                                        status=status,
                                        result=result_block.content,
                                        error=error,
                                        duration_ms=duration_ms,
                                        # Same rationale as the
                                        # ToolCallRequest above —
                                        # the call_id is the only
                                        # thing the adapter can
                                        # use to pair this output
                                        # back to its function_call.
                                        metadata=_call_metadata(result_block.tool_use_id, message),
                                    )

                    elif isinstance(message, sdk.ResultMessage):
                        result_msg = cast(_ResultMessageObj, message)
                        claude_session_id = getattr(result_msg, "session_id", None)
                        if getattr(result_msg, "is_error", None):
                            # Harness-level failure (e.g. expired login). Surface
                            # as an executor error rather than assistant content.
                            failure_text = result_msg.result or "claude-sdk harness error"
                            logger.error(
                                "claude-sdk ResultMessage is_error=True for agent %r: %s",
                                self._agent_name,
                                failure_text,
                            )
                            terminal_error = failure_text
                            terminal_code = classify_provider_failure(
                                error_type=provider_error_type,
                                status=getattr(result_msg, "api_error_status", None),
                                # Provider translation, last resort: the CLI reports some
                                # failures with no structured marker.
                                message=failure_text,
                            )
                        else:
                            # A later CLI turn (a steer that missed the first) succeeded:
                            # the turn ends on its answer, not an earlier error.
                            terminal_error = None
                            terminal_code = None
                            provider_error_type = None
                            if not response_text and result_msg.result:
                                response_text = result_msg.result
                        raw_usage = getattr(result_msg, "usage", None)
                        if isinstance(raw_usage, dict) and raw_usage:
                            # ``ResultMessage.usage`` is CUMULATIVE across every
                            # API call of one CLI turn. A steer the CLI answered as
                            # its own next turn adds a second result: sum them so
                            # every billing bucket (cache buckets included) is priced.
                            if billed_usage is not None:
                                raw_usage = _sum_usage(billed_usage, raw_usage)
                            billed_usage = raw_usage
                            in_tok = raw_usage.get("input_tokens") or 0
                            out_tok = raw_usage.get("output_tokens") or 0
                            # ``context_tokens`` is window FILL, not a billing
                            # sum: it must be a single prompt's size, not the
                            # cumulative input across iterations (which re-sends
                            # the conversation each tool-loop step and would
                            # over-count K-fold on a K-call turn). Use the LAST
                            # ``message_start`` call's prompt — input +
                            # cache_creation + cache_read — which already holds
                            # the full conversation carried into the next turn.
                            # So it is never summed across results either: the last
                            # call's prompt already carries any earlier CLI turn.
                            # Fall back to the cumulative sum only when no
                            # ``message_start`` was observed (e.g. non-streaming
                            # paths), preserving a non-null value over None.
                            ctx_src = last_call_usage if last_call_usage is not None else raw_usage
                            ctx_in = ctx_src.get("input_tokens") or 0
                            ctx_cc = ctx_src.get("cache_creation_input_tokens") or 0
                            ctx_cr = ctx_src.get("cache_read_input_tokens") or 0
                            turn_usage = {
                                "input_tokens": in_tok,
                                "output_tokens": out_tok,
                                "total_tokens": in_tok + out_tok,
                                "context_tokens": ctx_in + ctx_cc + ctx_cr,
                                **{
                                    k: v
                                    for k, v in raw_usage.items()
                                    if k
                                    not in (
                                        "input_tokens",
                                        "output_tokens",
                                        "context_tokens",
                                    )
                                },
                                # Harness-reported model for cost pricing: prefer the SDK's
                                # observed_model (config model is None when no model is pinned).
                                "model": observed_model or model,
                            }

                    elif isinstance(message, sdk.SystemMessage):
                        system_msg = cast(_SystemMessageObj, message)
                        subtype = system_msg.subtype
                        data = system_msg.data

                        if subtype == "api_retry":
                            error_status = data.get("error_status")
                            retry_error = data.get("error", "unknown_error")
                            attempt = data.get("attempt")
                            max_retries = data.get("max_retries")
                            retry_delay_ms = data.get("retry_delay_ms")
                            diagnostic = (
                                "Claude CLI API retry"
                                f" {attempt}/{max_retries}: {retry_error}"
                                f" (status={error_status}, retry_delay_ms={retry_delay_ms})"
                            )
                            system_diagnostics.append(diagnostic)
                            logger.warning(diagnostic)

                            if (
                                error_status in {401, 403}
                                or retry_error == "authentication_failed"
                            ):
                                if self._gateway:
                                    auth_hint = (
                                        "Check your provider's base URL and auth command "
                                        "(ANTHROPIC_BASE_URL / gateway auth)."
                                    )
                                else:
                                    auth_hint = (
                                        "Check your Claude CLI login status "
                                        "(`claude /status`) or API key configuration."
                                    )
                                terminal_error = (
                                    "Claude SDK provider authentication failed"
                                    f" ({retry_error}, status={error_status}). "
                                    f"{auth_hint}"
                                )
                                terminal_code = AUTH_FAILED
                                break

                            if error_status == 404:
                                if self._gateway:
                                    endpoint_hint = (
                                        "Check ANTHROPIC_BASE_URL / gateway endpoint "
                                        "configuration."
                                    )
                                else:
                                    endpoint_hint = "Check ANTHROPIC_BASE_URL configuration."
                                terminal_error = (
                                    "Claude SDK provider endpoint was not found "
                                    f"({retry_error}, status={error_status}). "
                                    f"{endpoint_hint}"
                                )
                                terminal_code = PROVIDER_UNAVAILABLE
                                break
                        elif getattr(system_msg, "hook_event_name", None) == "PreCompact":
                            compaction_occurred = True
                            hook_session_id = getattr(system_msg, "session_id", None)
                            if hook_session_id is None and isinstance(data, dict):
                                hook_session_id = data.get("session_id")
                            if hook_session_id:
                                claude_session_id = str(hook_session_id)
                            hook_transcript_path = getattr(system_msg, "transcript_path", None)
                            if hook_transcript_path is None and isinstance(data, dict):
                                hook_transcript_path = data.get("transcript_path")
                            if isinstance(hook_transcript_path, str) and hook_transcript_path:
                                compaction_transcript_path = pathlib.Path(hook_transcript_path)
                                try:
                                    compaction_transcript_offset = (
                                        compaction_transcript_path.stat().st_size
                                    )
                                except OSError:
                                    compaction_transcript_offset = None
                            logger.info("Claude SDK compaction detected (PreCompact hook)")
                            from omnigent.harnesses.executor import CompactionStarted

                            yield CompactionStarted()
                        else:
                            logger.info("Claude CLI system message: %s", data)
                            if isinstance(data, dict) and data.get("subtype") == "init":
                                from omnigent.harnesses import llm_trace

                                llm_trace.event(session_key, "cli_init", data)
            finally:
                # ``receive_response`` returns an async generator, which
                # always has ``aclose``; guard anyway for duck-typed test
                # doubles that implement the iterator protocol without it.
                aclose = getattr(message_stream, _ACLOSE_ATTR, None)
                if aclose is not None:
                    await aclose()

        except asyncio.CancelledError:
            # CancelledError is a BaseException, so a watchdog-cancelled turn
            # skips the boundary below. Evict the wedged client (no crash
            # mark) so the next turn rebuilds a fresh one and replays history
            # instead of reusing it and re-tripping the watchdog (#2109).
            self._evict_client_on_cancel(session_key)
            raise
        except Exception as exc:  # noqa: BLE001 — top-level executor error boundary; records crash and surfaces to caller
            self._pending_framework_context.pop(session_key, None)
            if session_key in self._interrupted_sessions:
                # The interrupt closed the CLI under this read: a cancelled turn, not a crash.
                for refused in self._settle_steer_window(session_key, steer_window):
                    yield refused
                yield TurnCancelled()
                return
            self._crashed_sessions[session_key] = str(exc)
            await self._close_live_client(session_key, "the previous turn failed")
            for refused in self._settle_steer_window(session_key, steer_window):
                yield refused
            stderr_text = "\n".join(stderr_lines) if stderr_lines else "(no stderr captured)"
            diagnostics_text = (
                "\n".join(system_diagnostics)
                if system_diagnostics
                else "(no system diagnostics captured)"
            )
            logger.error(
                "ClaudeSDKExecutor error: %s\nCLI stderr:\n%s\nCLI system diagnostics:\n%s",
                exc,
                stderr_text,
                diagnostics_text,
            )
            if compaction_occurred and claude_session_id:
                compaction_event = _build_compaction_complete_event(
                    require_new_compact_summary=True
                )
                if compaction_event is not None:
                    yield compaction_event
            yield ExecutorError(
                message=(
                    f"Claude SDK error: {exc}\n"
                    f"CLI stderr:\n{stderr_text}\n"
                    f"CLI system diagnostics:\n{diagnostics_text}"
                ),
                usage=turn_usage
                if turn_usage is not None
                else _usage_from_observed_call(last_call_usage, observed_model or model),
            )
            return
        finally:
            self._pending_framework_context.pop(session_key, None)
            closing = self._steer_windows.pop(session_key, None)
            if closing is not None:
                closing.open = False
                if closing.pending:
                    # Cut short (cancelled, interrupted, closed) with steers in the CLI's queue:
                    # drop it so a later turn cannot answer them twice.
                    await self._close_live_client(
                        session_key, "the previous turn ended with a steer unanswered"
                    )
        # Steers the CLI never answered for (its stream ended early, or the loop stopped on a
        # terminal error): refused, so the caller's copy runs as the next turn.
        for refused in self._settle_steer_window(session_key, steer_window):
            yield refused
        # A turn can end without ``ResultMessage`` usage — the CLI can close
        # the stream early, fail terminally (auth failure, rejected retries),
        # or be cut short before its final usage is reported. In all of those
        # cases ``turn_usage`` is None and the context-occupancy meter would
        # freeze at the previous successful turn's value, hiding real window
        # fill exactly when a session is in trouble. The latest prompt size
        # was already observed from ``message_start`` (``last_call_usage``),
        # so synthesize a usage dict from it — for the failure return below
        # AND the completion path — and let the terminal event carry it.
        # ``output_tokens`` is unknown on an incomplete turn, so it reports 0
        # rather than guess. The full ``ResultMessage`` path above still wins
        # whenever it runs.
        if turn_usage is None:
            turn_usage = _usage_from_observed_call(last_call_usage, observed_model or model)
        if session_key in self._interrupted_sessions:
            yield TurnCancelled()
            return

        if provider_error_type and not terminal_error:
            # The CLI flagged a provider failure but sent no failing ResultMessage.
            terminal_error = (
                provider_error_text.strip() or f"provider error: {provider_error_type}"
            )
            terminal_code = classify_provider_failure(
                error_type=provider_error_type, message=provider_error_text
            )
        if terminal_error:
            if compaction_occurred and claude_session_id:
                compaction_event = _build_compaction_complete_event()
                if compaction_event is not None:
                    yield compaction_event
            yield ExecutorError(message=terminal_error, usage=turn_usage, code=terminal_code)
            return

        # ── LLM_RESPONSE policy evaluation ───────────────────────
        # Evaluate after the stream completes but before TurnComplete
        # so a DENY prevents the response from being persisted.
        if _policy_eval is not None:
            _resp_data: _JsonObject = {
                "model": model,
                "text_preview": (response_text[:500] if response_text else ""),
                "tool_calls_count": len(pending_tools),
            }
            if turn_usage is not None:
                _resp_data["usage"] = turn_usage
            _resp_verdict = await _policy_eval("PHASE_LLM_RESPONSE", _resp_data)
            if _resp_verdict.action == "POLICY_ACTION_DENY":
                _deny_reason = _resp_verdict.reason or "no reason given"
                if compaction_occurred and claude_session_id:
                    compaction_event = _build_compaction_complete_event()
                    if compaction_event is not None:
                        yield compaction_event
                yield ExecutorError(message=(f"LLM response denied by policy: {_deny_reason}"))
                return

        _notify_usage_from_dict(model=model, usage=turn_usage)

        if compaction_occurred and claude_session_id:
            compaction_event = _build_compaction_complete_event()
            if compaction_event is not None:
                yield compaction_event

        yield TurnComplete(response=response_text, usage=turn_usage)

    @staticmethod
    def _build_prompt(
        messages: list[Message],
        *,
        resume_session: bool,
    ) -> str | list[_JsonObject]:
        """
        Build the prompt for the SDK.

        For continued Claude SDK sessions, send only the latest user
        message. For a fresh session that already has replayable
        history (for example a sub-agent with
        ``pass_history=True``), serialize that history into the
        first prompt so Claude sees the prior conversation context.

        When the latest user message contains multimodal content
        (images, files), returns a list of Anthropic API content
        blocks instead of a plain string so the caller can send
        them through the SDK's structured message path.

        :param messages: Conversation history as inner
            :class:`Message` dicts.
        :param resume_session: ``True`` when the SDK session
            already has prior turns cached (no need to replay
            history).
        :returns: A plain string prompt, or a list of Anthropic
            API content block dicts when multimodal blocks are
            present in the latest user message.
        """
        if resume_session:
            return ClaudeSDKExecutor._extract_trailing_user_content(messages)

        user_messages = [msg for msg in messages if msg.get("role") == "user"]
        if len(messages) <= 1 or len(user_messages) <= 1:
            return ClaudeSDKExecutor._extract_latest_user_content(messages)

        # Check if the latest user message is multimodal — if so,
        # serialize prior history as a text prefix but preserve the
        # latest message's content blocks for native multimodal
        # delivery to the Anthropic API.
        latest_content = ClaudeSDKExecutor._extract_latest_user_content(messages)
        prior = messages[:-1] if messages else []

        prior_blocks: list[_JsonObject] = [_text_block("Conversation so far:")]
        for msg in prior:
            role = str(msg.get("role", "user")).replace("_", " ")
            raw_content = msg.get("content")
            rendered = [] if raw_content is None else _render_prior_content_blocks(raw_content)
            if not rendered:
                rendered = [_text_block("")]
            # Label the message inline when it opens with text; an attachment
            # that leads the message gets the label on its own line instead.
            if rendered[0].get("type") == "text":
                rendered[0] = _text_block(f"{role}: {rendered[0]['text']}")
            else:
                rendered.insert(0, _text_block(f"{role}:"))
            prior_blocks.extend(rendered)
        prior_blocks.append(_text_block(""))
        prior_blocks.append(
            _text_block(
                "Respond to the latest user message, using the conversation above as context."
            )
        )
        prior_blocks = _coalesce_text_blocks(prior_blocks)

        if isinstance(latest_content, list):
            return [*prior_blocks, *latest_content]
        # Coalescing leaves an all-text history as the single opening block, so
        # more than one block means an attachment survived and the prompt has
        # to stay structured for its bytes to reach the model.
        if len(prior_blocks) > 1:
            return [*prior_blocks, _text_block(f"\nuser: {latest_content}")]
        return f"{prior_blocks[0]['text']}\n\nuser: {latest_content}"

    @staticmethod
    def _extract_latest_user_content(
        messages: list[Message],
    ) -> str | list[_JsonObject]:
        """
        Extract the latest user message content for the SDK.

        Returns a plain string for text-only messages. When the
        message carries multimodal content blocks (``input_image``,
        ``input_file``), converts them to Anthropic API content
        block format and returns the list so the caller can send
        a structured message through the SDK transport.

        :param messages: Conversation history.
        :returns: A string prompt, or a list of Anthropic content
            block dicts.
        """
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content")
                if content is None:
                    return ""
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    return _to_anthropic_content_blocks(content)
                return str(content)
        return ""

    @staticmethod
    def _extract_trailing_user_content(
        messages: list[Message],
    ) -> str | list[_JsonObject]:
        """
        Extract the trailing run of consecutive user messages for the SDK.

        On a resumed SDK session the client already has all prior turns
        cached, so only the *new* user input is sent. Normally that is a
        single user message, but when the runner batches several buffered
        steered messages into one continuation turn (see
        ``_check_and_start_next_turn``), the tail of history holds more than
        one brand-new user message the SDK has never seen. Sending only the
        last (as :meth:`_extract_latest_user_content` does) silently drops
        the earlier ones. This collects every user message after the last
        non-user (assistant / tool) message and concatenates them so all
        newly-buffered input reaches the model.

        Text messages are joined with blank lines. If any message in the
        trailing run carries multimodal content blocks, the whole run is
        returned as a single list of Anthropic content blocks so the bytes
        survive.

        :param messages: Conversation history.
        :returns: A string prompt, or a list of Anthropic content
            block dicts when the trailing run is multimodal.
        """
        trailing: list[Message] = []
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content")
                if content == "/compact" or (
                    isinstance(content, list)
                    and len(content) == 1
                    and isinstance(content[0], dict)
                    and content[0].get("type") in {"text", "input_text"}
                    and content[0].get("text") == "/compact"
                ):
                    # The runner dispatches earlier input before a compact control.
                    # Compaction may emit no assistant text; keep it out of later prompts.
                    if not trailing:
                        return "/compact"
                    break
                trailing.append(msg)
            else:
                break
        trailing.reverse()
        if not trailing:
            return ""
        if len(trailing) == 1:
            return ClaudeSDKExecutor._extract_latest_user_content(trailing)

        # Convert each trailing user message to Anthropic content blocks.
        # If any block is non-text (image/document), keep the structured
        # list so the bytes reach the model; otherwise join the text.
        block_runs: list[list[_JsonObject]] = []
        has_non_text = False
        for msg in trailing:
            content = msg.get("content")
            if content is None:
                block_runs.append([])
            elif isinstance(content, str):
                block_runs.append([_text_block(content)])
            elif isinstance(content, list):
                converted = _to_anthropic_content_blocks(content)
                block_runs.append(converted)
                if any(b.get("type") != "text" for b in converted):
                    has_non_text = True
            else:
                block_runs.append([_text_block(str(content))])

        if has_non_text:
            merged: list[_JsonObject] = []
            for run in block_runs:
                merged.extend(run)
            return merged

        texts: list[str] = []
        for run in block_runs:
            for block in run:
                text = block.get("text")
                if block.get("type") == "text" and isinstance(text, str) and text:
                    texts.append(text)
        return "\n\n".join(texts)
