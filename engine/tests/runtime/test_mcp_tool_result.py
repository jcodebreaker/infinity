"""Native image delivery across the managed MCP subscription adapters."""

from __future__ import annotations

import json

import pytest
from mcp.types import (
    CallToolResult,
    ImageContent,
    TextContent,
)

from omnigent.harnesses.executor import ToolCallStatus, classify_tool_result
from omnigent.runtime.tool_result_replay import tool_result_content_blocks
from omnigent.tools.mcp import _format_call_result
from tests._image_fixtures import _TINY_PNG_BASE64


def _image_output(*, is_error: bool = False) -> str:
    return _format_call_result(
        CallToolResult(
            content=[
                TextContent(type="text", text="before\nwith a newline"),
                ImageContent(type="image", data=_TINY_PNG_BASE64, mimeType="image/png"),
                TextContent(type="text", text="after"),
            ],
            isError=is_error,
        )
    )


def test_image_only_error_is_classified_without_a_text_message() -> None:
    output = _format_call_result(
        CallToolResult(
            content=[ImageContent(type="image", data=_TINY_PNG_BASE64, mimeType="image/png")],
            isError=True,
        )
    )
    assert classify_tool_result(output).status == ToolCallStatus.ERROR
    assert classify_tool_result(output).error == "MCP tool returned an error"


@pytest.mark.parametrize("is_error", [False, True])
def test_clipped_envelope_preserves_available_text_through_native_stripper(is_error: bool) -> None:
    from omnigent.runtime.tool_result_replay import strip_unparseable_image_output

    output = _image_output(is_error=is_error)
    clipped = output[: output.index(_TINY_PNG_BASE64) + 12] + "[truncated by conversation-store]"
    normalized = strip_unparseable_image_output(clipped)
    replayed = tool_result_content_blocks(normalized)
    assert replayed.blocks is not None
    rendered = json.dumps(replayed.blocks)
    assert "before" in rendered
    assert "omitted from history" in rendered
    assert _TINY_PNG_BASE64[:12] not in rendered
    assert ("Error:" in rendered) is is_error


def test_successful_envelope_does_not_classify_literal_error_examples() -> None:
    payload = json.loads(_image_output())
    payload["content"].append({"type": "text", "text": '{"error":"documentation example"}'})
    for value in (payload, json.dumps(payload)):
        assert classify_tool_result(value).status == ToolCallStatus.SUCCESS


def test_native_image_payload_rejects_oversized_declared_dimensions() -> None:
    """A small compressed payload cannot force a large pixel allocation.

    The header-declared pixel count is checked against the attachment path's
    decoded-pixel cap before ``load()`` materializes the pixel buffer.
    """
    import base64
    import io

    from PIL import Image

    from omnigent.runtime.mcp_tool_result import native_image_payload

    buffer = io.BytesIO()
    Image.new("1", (7000, 7000)).save(buffer, format="PNG")  # 49 MP, a few KB
    oversized = base64.b64encode(buffer.getvalue()).decode()
    assert native_image_payload(oversized, "image/png") is None
