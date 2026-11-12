"""The ``look`` an HTML page's save reports: how what changed shows in a real render.

The host's headless Chromium (the deck helper's warm browser) renders the saved page at a desktop
size, finds the elements that differ from the version before (on a first version, the named
ones) and measures each from the rendered pixels: shown or not, on the page, covered, and how
clearly it stands out from what is behind it. The model reports what this saw.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omnigent.superchat.artifact_kinds import is_deck_name, kind_for_name

if TYPE_CHECKING:
    from omnigent.superchat.feature import HandlerCtx


def is_page(name: str) -> bool:
    """An HTML page or app (a deck has its own ``layout`` check)."""
    return kind_for_name(name) == "html" and not is_deck_name(name)


async def saved_look(
    data: bytes, before: bytes | None, ctx: HandlerCtx | None, source: Path | None = None
) -> dict[str, Any]:
    """The ``look`` of a saved page: ``{"checked": true, "ok", "scope", "seen": [...]}`` or
    ``{"checked": false, "why"}``.

    :param data: The saved page.
    :param before: The version before it, or ``None`` for a first version.
    :param source: The page's workspace file when it holds ``data``: rendered in place, so the
        images and styles it links next to it load.
    """
    from omnigent.superchat.decks.handlers import run_helper

    try:
        with tempfile.TemporaryDirectory(prefix="superchat-page-look-") as tmp:
            page = source
            if page is None:
                page = Path(tmp) / "page.html"
                await asyncio.to_thread(page.write_bytes, data)
            previous = None
            if before is not None:
                previous = Path(tmp) / "before.html"
                await asyncio.to_thread(previous.write_bytes, before)
            result = await run_helper("look", page, ctx=ctx, before=previous)
    except Exception as exc:  # noqa: BLE001 - the save stands; say the look did not run
        return {"checked": False, "why": f"The page could not be rendered: {exc}"[:300]}
    if isinstance(result, str):
        return {"checked": False, "why": result}
    changed = result.get("changed")
    seen = [str(line) for line in result.get("elements") or []]
    look: dict[str, Any] = {
        "checked": True,
        "ok": not result.get("problems"),
        "scope": "page" if changed is None else "changed",
        "seen": seen,
    }
    if changed == 0:
        look["note"] = "Nothing new is drawn in this version (only removals or no visible change)."
    elif changed is None and not seen:
        look["note"] = "Every named part of the page is shown and stands out from its background."
    return look
