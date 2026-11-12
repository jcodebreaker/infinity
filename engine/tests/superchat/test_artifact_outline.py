"""``artifact_outline``: the Conversation reads a saved file to review a Helper (ADR 0027)."""

from __future__ import annotations

import io
import json

import httpx
from openpyxl import Workbook

from omnigent.context.labels import (
    ARTIFACT_CHAT_LABEL_KEY,
    CONTEXT_MODE_LABEL,
    SUBAGENT_LABEL_KEY,
    SUPERSIDE_CHAT_MODE_VALUE,
)
from omnigent.superchat.artifacts.feature import _tools
from omnigent.superchat.artifacts.handlers import handle_artifact_tool, outline_text
from omnigent.superchat.decks.outline import HEADER, MAX_CHARS
from omnigent.superchat.feature import HandlerCtx, ToolManagerCtx
from omnigent.superchat.work_tools import is_work_tool

CHAT = {CONTEXT_MODE_LABEL: SUPERSIDE_CHAT_MODE_VALUE}
ARTIFACT_ID = "a" * 32


def _names(labels: dict[str, str]) -> set[str]:
    return {tool.name() for tool in _tools(labels, ToolManagerCtx())}


def test_offered_to_the_conversation_only() -> None:
    assert "artifact_outline" in _names(CHAT)
    assert "artifact_outline" not in _names({**CHAT, SUBAGENT_LABEL_KEY: "worker"})
    assert "artifact_outline" not in _names({**CHAT, ARTIFACT_CHAT_LABEL_KEY: ARTIFACT_ID})


def test_outline_is_a_read_never_counted() -> None:
    assert not is_work_tool("artifact_outline")
    assert not is_work_tool("mcp__omnigent__artifact_outline")


def _deck(slides: int) -> bytes:
    body = "".join(
        f'<section class="slide"><h1 data-superchat-id="title-{n}">Slide title {n} '
        f"{'word ' * 12}</h1></section>"
        for n in range(slides)
    )
    return f"<html><body>{body}</body></html>".encode()


def test_deck_outline_is_capped() -> None:
    outline = outline_text("q3.deck.html", "html", _deck(40))
    assert outline.startswith(HEADER)
    assert "Slide 1: title-0" in outline
    assert len(outline) <= MAX_CHARS


def test_text_shows_the_first_sixty_lines() -> None:
    data = "\n".join(f"line {n}" for n in range(100)).encode()
    outline = outline_text("notes.md", "md", data)
    assert "line 59" in outline and "line 60" not in outline
    assert "(40 more lines)" in outline


def test_sheet_shows_its_headers() -> None:
    assert "Region | Revenue" in outline_text("t.csv", "csv", b"Region,Revenue\nEU,3\nUS,4\n")
    book = Workbook()
    sheet = book.active
    sheet.title = "Sales"
    sheet.append(["Month", "Units"])
    sheet.append(["Jan", 3])
    buf = io.BytesIO()
    book.save(buf)
    outline = outline_text("t.xlsx", "xlsx", buf.getvalue())
    assert "Sales (2 rows, 2 columns): Month | Units" in outline


def test_binary_kinds_say_so() -> None:
    assert "no text outline" in outline_text("a.pdf", "pdf", b"%PDF")


async def test_handler_reads_the_newest_version() -> None:
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        path = request.url.path
        if path == "/v1/sessions/chat":
            return httpx.Response(200, json={"kind": "conversation"})
        if path == f"/v1/artifacts/{ARTIFACT_ID}":
            return httpx.Response(
                200,
                json={
                    "name": "q3.deck.html",
                    "kind": "html",
                    "group_id": ARTIFACT_ID,
                    "version": 1,
                    "all_versions": [{"version": 3}, {"version": 2}],
                },
            )
        if path == f"/v1/artifacts/{ARTIFACT_ID}/content":
            return httpx.Response(200, content=_deck(2))
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://engine"
    ) as client:
        ctx = HandlerCtx("artifact_outline", client, "chat", CHAT)
        out = json.loads(await handle_artifact_tool(ctx, {"artifact_id": ARTIFACT_ID}))
        bad = json.loads(await handle_artifact_tool(ctx, {"artifact_id": "nope"}))
    assert out["version"] == 3 and out["name"] == "q3.deck.html"
    assert out["outline"].startswith(HEADER)
    assert any(url.endswith("/content?version=3") for url in seen)
    assert "error" in bad
