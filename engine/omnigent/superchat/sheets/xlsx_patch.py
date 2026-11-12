"""Lossless cell edits for XLSX files: splice the target ``<c>`` elements, copy the rest.

openpyxl cannot round-trip a workbook (it drops charts, pivot tables, sparklines, slicers and
images, and an edited formula loses its cached value), so edits never go through its writer.
This module opens the zip, finds each edited sheet's ``xl/worksheets/sheetN.xml`` and rewrites
only the ``<c>`` elements being changed (plus the ``<row>`` that holds a new cell and the
``<dimension>`` hint); every other zip entry is copied with identical content, order, timestamps
and compression type. ``<calcPr fullCalcOnLoad="1"/>`` makes Excel, LibreOffice and Sheets
recompute on open.

String splicing rather than an XML round-trip, on purpose: lxml is not a dependency, and
``xml.etree`` rewrites namespace prefixes (``x14ac:``, ``mc:Ignorable``...) and drops unknown
elements, which would corrupt exactly the parts this module exists to preserve. Cells and rows
never nest and their text is XML-escaped, so a tag scanner that skips quoted attribute values
is exact for the elements touched.

Edge cases handled: shared formulas (editing a master first expands its dependents into
ordinary formulas), cells with implicit coordinates, rows with ``spans`` (dropped on modified
rows), prefixed element names, and ``calcChain.xml`` (removed, with its content-type override and
relationship, only when an edit removes a formula, since a chain entry pointing at a non-formula
cell makes Excel offer to "repair" the file).
"""

from __future__ import annotations

import io
import math
import posixpath
import re
import zipfile
from collections.abc import Callable
from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

Value = str | int | float | bool | None

_ATTRS = r"""((?:[^>"']|"[^"]*"|'[^']*')*)"""
_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_NS_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


class XlsxPatchError(ValueError):
    """The workbook cannot be patched (unknown sheet, unreadable structure)."""


def _attr(attrs: str, name: str) -> str | None:
    m = re.search(rf"""(?:^|\s){re.escape(name)}\s*=\s*(?:"([^"]*)"|'([^']*)')""", attrs)
    return None if m is None else (m.group(1) if m.group(1) is not None else m.group(2))


def _drop_attr(attrs: str, name: str) -> str:
    return re.sub(
        rf"""\s+{re.escape(name)}\s*=\s*(?:"[^"]*"|'[^']*')""", "", attrs, count=1
    ).rstrip("/")


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _split_ref(ref: str) -> tuple[int, int]:
    m = re.fullmatch(r"([A-Za-z]{1,3})(\d+)", ref)
    if m is None:
        raise XlsxPatchError(f"bad cell reference: {ref!r}")
    return int(m.group(2)) - 1, _col_index(m.group(1))


def _letters(col: int) -> str:
    out, n = "", col + 1
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


# ------------------------------------------------------------------------- element scanner


def _scan(text: str, tag: str, prefix: str) -> list[tuple[int, int, str, str]]:
    """Top-level ``<tag ...>...</tag>`` / ``<tag .../>`` spans: ``(start, end, attrs, inner)``."""
    name = re.escape(prefix + tag)
    opener = re.compile(rf"<{name}(?=[\s/>]){_ATTRS}>")
    out: list[tuple[int, int, str, str]] = []
    pos = 0
    while True:
        m = opener.search(text, pos)
        if m is None:
            return out
        attrs = m.group(1)
        if attrs.endswith("/"):
            out.append((m.start(), m.end(), attrs[:-1], ""))
            pos = m.end()
            continue
        close = text.find(f"</{prefix}{tag}>", m.end())
        if close < 0:
            raise XlsxPatchError(f"unterminated <{tag}> element")
        end = close + len(f"</{prefix}{tag}>")
        out.append((m.start(), end, attrs, text[m.end() : close]))
        pos = end


# ------------------------------------------------------------------------------ cell build


def _cell_xml(prefix: str, ref: str, attrs: str, value: Value) -> tuple[str, bool, bool]:
    """A rebuilt ``<c>`` plus ``(has_formula, is_empty)``. ``attrs`` keeps everything but ``t``."""
    keep = _drop_attr(_drop_attr(attrs, "t"), "r")
    head = f'<{prefix}c r="{ref}"{keep}'
    if value is None:
        return f"{head}/>", False, True
    if isinstance(value, bool):
        return f'{head} t="b"><{prefix}v>{int(value)}</{prefix}v></{prefix}c>', False, False
    if isinstance(value, int | float):
        if isinstance(value, float) and not math.isfinite(value):
            raise XlsxPatchError("numbers must be finite")
        body = repr(value)
        return f"{head}><{prefix}v>{body}</{prefix}v></{prefix}c>", False, False
    text = _BAD_XML.sub("", value)
    if text.startswith("=") and len(text) > 1:
        return f"{head}><{prefix}f>{escape(text[1:])}</{prefix}f></{prefix}c>", True, False
    return (
        f'{head} t="inlineStr"><{prefix}is><{prefix}t xml:space="preserve">{escape(text)}'
        f"</{prefix}t></{prefix}is></{prefix}c>",
        False,
        False,
    )


def _formula_of(inner: str, prefix: str) -> tuple[str, str] | None:
    """``(attrs, text)`` of the cell's ``<f>`` element, if any."""
    found = _scan(inner, "f", prefix)
    return None if not found else (found[0][2], found[0][3])


# ------------------------------------------------------------------------------- sheet edit


def _expand_shared(body: str, prefix: str, sis: set[str]) -> str:
    """Turn the dependents of the given shared-formula groups into ordinary formulas."""
    from openpyxl.formula.translate import Translator

    cells = _scan(body, "c", prefix)
    masters: dict[str, tuple[str, str]] = {}
    for _, _, attrs, inner in cells:
        f = _formula_of(inner, prefix)
        if f is None or _attr(f[0], "t") != "shared":
            continue
        si = _attr(f[0], "si")
        ref = _attr(attrs, "r")
        if si in sis and ref and f[1].strip():
            masters[si] = (ref, f[1])
    out: list[str] = []
    last = 0
    for start, end, attrs, inner in cells:
        f = _formula_of(inner, prefix)
        si = _attr(f[0], "si") if f else None
        if f and _attr(f[0], "t") == "shared" and si in masters and not f[1].strip():
            ref = _attr(attrs, "r")
            origin, text = masters[si]  # type: ignore[index]
            if ref:
                from xml.sax.saxutils import unescape

                moved = Translator("=" + unescape(text), origin=origin).translate_formula(ref)
                new_f = f"<{prefix}f>{escape(moved[1:])}</{prefix}f>"
                fm = _scan(inner, "f", prefix)[0]
                new_inner = inner[: fm[0]] + new_f + inner[fm[1] :]
                out.append(body[last:start])
                out.append(f"<{prefix}c{attrs}>{new_inner}</{prefix}c>")
                last = end
    out.append(body[last:])
    return "".join(out)


def _edit_row(
    raw: str, prefix: str, row_idx: int, edits: dict[int, Value], lost: list[bool]
) -> str:
    (start, end, attrs, inner), *_ = _scan(raw, "row", prefix)
    cells = _scan(inner, "c", prefix)
    parts: list[tuple[int, str]] = []
    next_col = 0
    for cs, ce, cattrs, cinner in cells:
        ref = _attr(cattrs, "r")
        col = _split_ref(ref)[1] if ref else next_col
        next_col = col + 1
        raw_cell = inner[cs:ce]
        if col in edits:
            f = _formula_of(cinner, prefix)
            new, has_f, _ = _cell_xml(
                prefix, f"{_letters(col)}{row_idx + 1}", cattrs, edits.pop(col)
            )
            if f is not None and not has_f:
                lost[0] = True
            raw_cell = new
        parts.append((col, raw_cell))
    for col, value in edits.items():
        new, _, empty = _cell_xml(prefix, f"{_letters(col)}{row_idx + 1}", "", value)
        if not empty:
            parts.append((col, new))
    parts.sort(key=lambda p: p[0])
    row_attrs = _drop_attr(attrs, "spans")
    return (
        raw[:start] + f"<{prefix}row{row_attrs}>" + "".join(p[1] for p in parts)
        + f"</{prefix}row>" + raw[end:]
    )  # fmt: skip


def _patch_sheet(xml: str, edits: dict[tuple[int, int], Value]) -> tuple[str, bool]:
    """Apply ``{(row, col): value}`` to one sheet's XML; returns it and "a formula was removed"."""
    m = re.search(rf"<((?:[\w.-]+:)?)sheetData(?=[\s/>]){_ATTRS}>", xml)
    if m is None:
        raise XlsxPatchError("sheet has no <sheetData>")
    prefix = m.group(1)
    if m.group(2).endswith("/"):
        body = ""
        head = xml[: m.start()]
        sd_open = f"<{prefix}sheetData{m.group(2)[:-1]}>"
        tail = xml[m.end() :]
    else:
        close = xml.find(f"</{prefix}sheetData>", m.end())
        if close < 0:
            raise XlsxPatchError("unterminated <sheetData>")
        body = xml[m.end() : close]
        head = xml[: m.start()]
        sd_open = xml[m.start() : m.end()]
        tail = xml[close + len(f"</{prefix}sheetData>") :]
    by_row: dict[int, dict[int, Value]] = {}
    for (r, c), v in edits.items():
        by_row.setdefault(r, {})[c] = v

    rows = _scan(body, "row", prefix)
    # Shared formulas whose master is being replaced would orphan their dependents.
    sis: set[str] = set()
    for _, _, rattrs, rinner in rows:
        rnum = _attr(rattrs, "r")
        targets = by_row.get(int(rnum) - 1) if rnum else None
        if not targets:
            continue
        for _, _, cattrs, cinner in _scan(rinner, "c", prefix):
            ref = _attr(cattrs, "r")
            f = _formula_of(cinner, prefix)
            if ref and f and _attr(f[0], "t") == "shared" and f[1].strip():
                if _split_ref(ref)[1] in targets:
                    sis.add(_attr(f[0], "si") or "")
    if sis:
        body = _expand_shared(body, prefix, sis)
        rows = _scan(body, "row", prefix)

    lost = [False]
    out: list[str] = []
    last = 0
    implicit = 0
    pending = sorted(by_row)

    def new_row(idx: int) -> str:
        return _edit_row(f'<{prefix}row r="{idx + 1}"/>', prefix, idx, by_row[idx], lost)

    for rs, re_, rattrs, _ in rows:
        rnum = _attr(rattrs, "r")
        idx = int(rnum) - 1 if rnum else implicit
        implicit = idx + 1
        while pending and pending[0] < idx:
            out.append(new_row(pending.pop(0)))
        out.append(body[last:rs])
        if idx in pending:
            out.append(_edit_row(body[rs:re_], prefix, idx, by_row[idx], lost))
            pending.remove(idx)
        else:
            out.append(body[rs:re_])
        last = re_
    out.append(body[last:])
    out.extend(new_row(idx) for idx in pending)
    new_xml = head + sd_open + "".join(out) + f"</{prefix}sheetData>" + tail
    return _fix_dimension(new_xml, edits), lost[0]


def _fix_dimension(xml: str, edits: dict[tuple[int, int], Value]) -> str:
    m = re.search(r'(<(?:[\w.-]+:)?dimension\b[^>]*?\bref=")([^"]*)(")', xml)
    if m is None:
        return xml
    cells = [(r, c) for (r, c), v in edits.items() if v is not None]
    if not cells:
        return xml
    parts = m.group(2).split(":")
    try:
        r0, c0 = _split_ref(parts[0])
        r1, c1 = _split_ref(parts[-1])
    except XlsxPatchError:
        return xml
    r0, c0 = min(r0, *(r for r, _ in cells)), min(c0, *(c for _, c in cells))
    r1, c1 = max(r1, *(r for r, _ in cells)), max(c1, *(c for _, c in cells))
    ref = f"{_letters(c0)}{r0 + 1}:{_letters(c1)}{r1 + 1}"
    return xml[: m.start(2)] + ref + xml[m.end(2) :]


# --------------------------------------------------------------------------- workbook parts


def _sheet_paths(zf: zipfile.ZipFile) -> dict[str, str]:
    """``{sheet name: zip path of its worksheet part}``."""
    try:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    except (KeyError, ET.ParseError) as exc:
        raise XlsxPatchError(f"cannot read workbook structure: {exc}") from exc
    targets = {r.get("Id"): r.get("Target", "") for r in rels.iter(f"{_NS_PKG_REL}Relationship")}
    out: dict[str, str] = {}
    for sheet in wb.iter(f"{_NS_MAIN}sheet"):
        target = targets.get(sheet.get(f"{_NS_REL}id"))
        name = sheet.get("name")
        if not target or name is None:
            continue
        path = target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target)
        out[name] = posixpath.normpath(path)
    return out


def _set_full_calc(xml: str) -> str:
    m = re.search(rf"<((?:[\w.-]+:)?)calcPr(?=[\s/>]){_ATTRS}>", xml)
    if m is not None:
        attrs = m.group(2)
        selfclose = attrs.endswith("/")
        attrs = _drop_attr(attrs, "fullCalcOnLoad") + ' fullCalcOnLoad="1"'
        return (
            xml[: m.start()]
            + f"<{m.group(1)}calcPr{attrs}"
            + ("/>" if selfclose else ">")
            + xml[m.end() :]
        )
    root = re.search(r"<((?:[\w.-]+:)?)workbook\b", xml)
    prefix = root.group(1) if root else ""
    later = (
        "oleSize|customWorkbookViews|pivotCaches|smartTagPr|smartTagTypes|webPublishing|"
        "fileRecoveryPr|webPublishObjects|extLst"
    )
    tag = f'<{prefix}calcPr fullCalcOnLoad="1"/>'
    nxt = re.search(rf"<{re.escape(prefix)}(?:{later})(?=[\s/>])", xml)
    at = nxt.start() if nxt else xml.rfind(f"</{prefix}workbook>")
    if at < 0:
        raise XlsxPatchError("workbook.xml has no root element to extend")
    return xml[:at] + tag + xml[at:]


def _drop_calc_chain(name: str, data: bytes) -> bytes:
    text = data.decode("utf-8")
    if name == "[Content_Types].xml":
        text = re.sub(r"<Override\b[^>]*PartName=\"/xl/calcChain\.xml\"[^>]*/>", "", text)
    elif name == "xl/_rels/workbook.xml.rels":
        text = re.sub(r"<Relationship\b[^>]*Target=\"(?:/xl/)?calcChain\.xml\"[^>]*/>", "", text)
    return text.encode("utf-8")


def patch_cells(data: bytes, edits: list[tuple[str, int, int, Value]]) -> bytes:
    """Apply ``(sheet, row, col, value)`` edits (0-based) to XLSX ``data``, losslessly.

    ``None`` clears a cell's content (its style is kept), a string starting with ``=`` becomes a
    formula without a cached value, other strings are stored as inline strings.
    """
    try:
        zin = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise XlsxPatchError(f"not a valid .xlsx: {exc}") from exc
    with zin:
        paths = _sheet_paths(zin)
        per_part: dict[str, dict[tuple[int, int], Value]] = {}
        for sheet, row, col, value in edits:
            if sheet not in paths:
                raise XlsxPatchError(f"unknown sheet: {sheet}")
            per_part.setdefault(paths[sheet], {})[(row, col)] = value
        names = {i.filename for i in zin.infolist()}
        for part in per_part:
            if part not in names:
                raise XlsxPatchError(f"missing worksheet part: {part}")
        patched: dict[str, bytes] = {}
        lost_formula = False
        for part, cell_edits in per_part.items():
            xml, lost = _patch_sheet(zin.read(part).decode("utf-8"), cell_edits)
            patched[part] = xml.encode("utf-8")
            lost_formula = lost_formula or lost
        patched["xl/workbook.xml"] = _set_full_calc(
            zin.read("xl/workbook.xml").decode("utf-8")
        ).encode("utf-8")
        rewrite: dict[str, Callable[[bytes], bytes]] = {}
        if lost_formula and "xl/calcChain.xml" in names:
            for part in ("[Content_Types].xml", "xl/_rels/workbook.xml.rels"):
                rewrite[part] = lambda raw, part=part: _drop_calc_chain(part, raw)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as zout:
            for info in zin.infolist():
                if lost_formula and info.filename == "xl/calcChain.xml":
                    continue
                blob = patched.get(info.filename)
                if blob is None:
                    blob = zin.read(info.filename)
                    fix = rewrite.get(info.filename)
                    if fix is not None:
                        blob = fix(blob)
                clone = zipfile.ZipInfo(info.filename, info.date_time)
                clone.compress_type = info.compress_type
                clone.external_attr = info.external_attr
                clone.comment = info.comment
                clone.create_system = info.create_system
                zout.writestr(clone, blob)
        return out.getvalue()


__all__: list[Any] = ["XlsxPatchError", "patch_cells"]
