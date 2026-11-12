"""A fake Omnigent server for deck_new's look: the view of one chat and a Memory Profile."""

from __future__ import annotations

from typing import Any

import httpx

from omnigent.superchat.feature import HandlerCtx

SESSION = "conv_look"


def view(
    message: str = "",
    *,
    answer: str | None = None,
    option_id: str = "o1",
    background: bool = False,
    is_helper: bool = False,
) -> dict[str, Any]:
    """What ``GET /v1/sessions/{id}/tasks`` answers (:meth:`TaskService.view`)."""
    return {
        "conversation_id": SESSION,
        "is_helper": is_helper,
        "background": background,
        "message": message,
        "answer": (
            {"card_id": "c1", "option_id": option_id, "label": answer}
            if answer is not None
            else None
        ),
        "round_id": "r1",
        "rounds": [],
    }


def ctx(task_view: dict[str, Any] | None = None, *, profile: str | None = None) -> HandlerCtx:
    """A ``deck_new`` call whose server holds *task_view* and the owner's Memory *profile*."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/v1/sessions/{SESSION}/tasks" and task_view is not None:
            return httpx.Response(200, json=task_view)
        if path == f"/v1/sessions/{SESSION}/memory/profile":
            return httpx.Response(200, json={"profile": profile})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://srv")
    return HandlerCtx(tool_name="deck_new", server_client=client, conversation_id=SESSION)


def helper_ctx() -> HandlerCtx:
    """A scheduled Helper: background work, no person to ask."""
    return ctx(view(background=True, is_helper=True))
