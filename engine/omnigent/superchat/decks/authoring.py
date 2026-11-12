"""Runner-side handlers for ``deck_new`` and ``deck_check``: authoring a deck that exports exactly.

``deck_new`` assembles the deck file (skeleton, template, embedded fonts, the Muse's slides) so the
model only ever writes slides; ``deck_check`` measures the laid-out deck in the host's Chromium
(the same render the PowerPoint export reads) and reports what would not survive one-to-one.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omnigent.superchat._handler_http import error
from omnigent.superchat.artifacts import workspace_roots
from omnigent.superchat.decks import kit
from omnigent.superchat.decks.handlers import run_helper
from omnigent.superchat.decks.look import Look, settle_look
from omnigent.superchat.decks.names import DECK_SUFFIX, is_deck_name

if TYPE_CHECKING:
    from omnigent.superchat.feature import HandlerCtx

_MAX_LISTED = 25
_SEVERITY_BUCKET = {"error": "errors", "warning": "warnings", "note": "notes"}


def _resolve(raw: str, *, must_exist: bool) -> Path | str:
    """The deck file path under a workspace root, or an error sentence."""
    if not is_deck_name(Path(raw).name):
        return f"The file name must end with {DECK_SUFFIX}"
    roots = workspace_roots()
    if not roots:
        return "There is no workspace to write in"
    given = Path(raw).expanduser()
    candidates = [given] if given.is_absolute() else [root / given for root in roots]
    for candidate in candidates:
        try:
            real = candidate.resolve()
        except OSError:
            continue
        if not any(real.is_relative_to(root) for root in roots):
            continue
        if real.is_file() or not must_exist:
            return real
    if must_exist:
        return f"File not found: {raw}"
    return "The path must be inside your workspace"


def _issue_line(issue: dict[str, Any]) -> str:
    where = f"Slide {issue.get('slide')}"
    label = str(issue.get("label") or "")
    if label:
        where += f" ({label.split(' ', 1)[-1]})"
    target = f" [{issue['id']}]" if issue.get("id") else ""
    return f"{where}{target}: {issue.get('message')}"


async def _layout_report(path: Path, ctx: HandlerCtx | None) -> dict[str, Any]:
    """Measure the deck in Chromium: ``errors``, ``warnings``, ``notes``, ``layout_checked``."""
    result = await run_helper("lint", path, ctx=ctx)
    if isinstance(result, str):
        return {"layout_checked": False, "layout_note": result}
    buckets: dict[str, list[str]] = {"errors": [], "warnings": [], "notes": []}
    for issue in result.get("issues") or []:
        bucket = _SEVERITY_BUCKET.get(str(issue.get("severity")), "warnings")
        buckets[bucket].append(_issue_line(issue))
    out: dict[str, Any] = {"layout_checked": True, "slides": result.get("slides")}
    for key, lines in buckets.items():
        out[key] = lines[:_MAX_LISTED]
        if len(lines) > _MAX_LISTED:
            out[key].append(f"...and {len(lines) - _MAX_LISTED} more")
    return out


def _verdict(report: dict[str, Any]) -> dict[str, Any]:
    ok = not report.get("errors")
    report["ok"] = ok
    report["next"] = (
        "Layout is clean. Save it with artifact_save, then the person can export it."
        if ok
        else "Fix the errors in the file (edit the slides), then run deck_check again."
    )
    return report


def _look_note(look: Look, written: bool) -> str:
    """What the model is told about a look the engine chose."""
    name = kit.templates()[look.theme].name
    if written:
        return (
            f"Built in {name}, chosen for the topic: {look.why}. The file card's Theme control "
            "changes the look later; do not ask about the look."
        )
    return (
        f"Use template {look.theme} ({name}), chosen for the topic ({look.why}), when you add "
        "the slides."
    )


def _deck_record(look: Look, title: str, slides: object, target: Path | None) -> dict[str, Any]:
    """What the result records about this deck (``deck``)."""
    count = slides.count('<section class="slide') if isinstance(slides, str) else 0
    record: dict[str, Any] = {
        "title": title.strip(),
        "look": kit.templates()[look.theme].name,
        "look_id": look.theme,
        "look_from": look.look_from,
        "slides": count,
    }
    if target is not None:
        record["file"] = target.name
    return record


async def handle_deck_new(args: dict[str, Any], ctx: HandlerCtx | None = None) -> str:
    """Assemble a new deck file from a template and the Muse's slides, then check it.

    Any valid ``template`` is built and ``look_from`` recorded as given. With none, the look is
    the theme picked on the task's card, else one chosen for the topic
    (:mod:`~omnigent.superchat.decks.look`), and the result says which.
    """
    raw = args.get("path")
    template = args.get("template")
    title = args.get("title")
    slides = args.get("slides")
    if not isinstance(raw, str) or not raw.strip():
        return error("deck_new requires a path ending in .deck.html")
    if not isinstance(title, str) or not title.strip():
        return error("deck_new requires a title")
    if template is not None and (not isinstance(template, str) or template not in kit.templates()):
        return error(f"deck_new template must be one of: {', '.join(kit.templates())}")
    look = await settle_look(ctx, args.get("look_from"), template, title)
    if look.refusal is not None:
        return error(look.refusal)
    if not isinstance(slides, str) or not slides.strip():
        # No slides: the record, and with ``layout`` one example. The layouts themselves are
        # the deck skill (ADR 0031), so nothing here repeats them per call or per session.
        layout = args.get("layout")
        reply: dict[str, Any] = {"ok": True, "written": False}
        if layout is not None:
            example = kit.layout_example(str(layout))
            if example is None:
                return error(f"deck_new layout must be one of: {', '.join(kit.layout_names())}")
            reply.update(layout=layout, example=example)
        reply["note"] = (
            "Nothing was written. Next: one deck_new call with the same path and title and all "
            "the slides."
        )
        if look.chosen:
            reply["note"] += " " + _look_note(look, written=False)
        reply["deck"] = _deck_record(look, title, None, None)
        return json.dumps(reply)
    target = _resolve(raw.strip(), must_exist=False)
    if isinstance(target, str):
        return error(target)
    if target.exists():
        return error(
            f"{raw} already exists. Edit its slides in place (never rewrite the whole file), "
            "then run deck_check."
        )
    errors, warnings = kit.check_slides(slides)
    if errors:
        return json.dumps({"ok": False, "written": False, "errors": errors, "warnings": warnings})
    try:
        document = kit.build_deck(look.theme, title, slides)
    except kit.KitError as exc:
        return error(str(exc))
    await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(target.write_text, document, "utf-8")
    report = await _layout_report(target, ctx)
    report["errors"] = report.get("errors", [])
    report["warnings"] = warnings + report.get("warnings", [])
    report.update(written=True, path=str(target), template=look.theme)
    report["deck"] = _deck_record(look, title, slides, target)
    if look.chosen:
        report["look_note"] = _look_note(look, written=True)
    return json.dumps(_verdict(report))


async def handle_deck_check(args: dict[str, Any], ctx: HandlerCtx | None = None) -> str:
    """Check an existing deck file: structure, ids, and layout in Chromium."""
    raw = args.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return error("deck_check requires the deck's path")
    target = _resolve(raw.strip(), must_exist=True)
    if isinstance(target, str):
        return error(target)
    document = await asyncio.to_thread(target.read_text, "utf-8")
    report = await _check_document(document, ctx)
    report["path"] = str(target)
    return json.dumps(_verdict(report))


async def _check_document(document: str, ctx: HandlerCtx | None) -> dict[str, Any]:
    """Structure, ids and layout of a deck's text: :func:`_layout_report` plus the slide rules."""
    start = document.find(kit.SLOTS_OPEN)
    end = document.find(kit.SLOTS_CLOSE)
    region = document[start + len(kit.SLOTS_OPEN) : end] if 0 <= start < end else document
    errors, warnings = kit.check_slides(region)
    # A scratch copy keeps the check independent of how the file is served.
    with tempfile.TemporaryDirectory(prefix="superchat-deck-check-") as tmp:
        copy = Path(tmp) / "deck.html"
        await asyncio.to_thread(copy.write_text, document, "utf-8")
        report = await _layout_report(copy, ctx)
    report["errors"] = errors + report.get("errors", [])
    report["warnings"] = warnings + report.get("warnings", [])
    return report


async def saved_layout(data: bytes, ctx: HandlerCtx | None = None) -> dict[str, Any]:
    """The ``layout`` a deck save reports: the same check as ``deck_check`` on the saved bytes.

    :returns: ``{"ok": true}`` or ``{"ok": false, "errors": [...]}`` (with ``warnings`` when
        there are any), or ``{"checked": false, "why": ...}`` (with any slide-rule ``errors``)
        when the render could not run.
    """
    try:
        report = await _check_document(data.decode("utf-8", "replace"), ctx)
    except Exception as exc:  # noqa: BLE001 - the save stands; say the check did not run
        return {"checked": False, "why": f"The layout check failed: {exc}"[:300]}
    layout: dict[str, Any] = {"ok": not report["errors"]}
    if report.get("layout_checked") is False:
        # Only the slide rules ran: no render, so nothing is said about fit.
        layout = {"checked": False, "why": report.get("layout_note")}
    if report["errors"]:
        layout["errors"] = report["errors"]
    if report["warnings"]:
        layout["warnings"] = report["warnings"]
    return layout


async def handle_deck_themes(_args: dict[str, Any]) -> str:
    """The theme dictionary: what the Muse picks from."""
    return json.dumps({"ok": True, "themes": kit.theme_dictionary(), "default": kit.DEFAULT_THEME})


async def handle_deck_theme_set(args: dict[str, Any]) -> str:
    """Swap a workspace deck's theme (slides untouched) and save it; no layout check.

    A check is a Chromium render; it runs when the model calls ``deck_check`` (docs/adr/0026).
    """
    raw = args.get("path")
    theme_id = args.get("theme_id")
    if not isinstance(raw, str) or not raw.strip():
        return error("deck_theme_set requires the deck's path")
    if not isinstance(theme_id, str) or theme_id not in kit.templates():
        return error(f"deck_theme_set theme_id must be one of: {', '.join(kit.templates())}")
    target = _resolve(raw.strip(), must_exist=True)
    if isinstance(target, str):
        return error(target)
    document = await asyncio.to_thread(target.read_text, "utf-8")
    before = kit.deck_theme_id(document)
    if before == theme_id:
        return json.dumps({"ok": True, "changed": False, "theme": theme_id, "path": str(target)})
    try:
        restyled = kit.restyle_deck(document, theme_id)
    except kit.KitError as exc:
        return error(str(exc))
    await asyncio.to_thread(target.write_text, restyled, "utf-8")
    return json.dumps(
        {"ok": True, "changed": True, "theme": theme_id, "previous": before, "path": str(target)}
    )


async def handle_authoring_tool(
    tool_name: str, args: dict[str, Any], ctx: HandlerCtx | None = None
) -> str:
    """Dispatch ``deck_new`` / ``deck_check`` / ``deck_themes`` / ``deck_theme_set``."""
    try:
        if tool_name == "deck_new":
            return await handle_deck_new(args, ctx)
        if tool_name == "deck_themes":
            return await handle_deck_themes(args)
        if tool_name == "deck_theme_set":
            return await handle_deck_theme_set(args)
        return await handle_deck_check(args, ctx)
    except Exception as exc:  # noqa: BLE001
        return error(f"{tool_name} failed: {exc}")


__all__ = [
    "handle_authoring_tool",
    "handle_deck_check",
    "handle_deck_new",
    "handle_deck_theme_set",
    "handle_deck_themes",
    "saved_layout",
]
