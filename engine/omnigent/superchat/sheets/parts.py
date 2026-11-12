"""A sheet's cells as the parts a live update reads and writes (``artifacts.parts``).

A part id is a cell: ``B3``, or ``Sheet2!B3`` in a workbook with several sheets (a CSV has one,
``Sheet1``). A cell holding a formula is never written: it follows its own inputs.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from omnigent.superchat.artifacts.parts import PART_EDITORS, PartEditor, PartError
from omnigent.superchat.sheets import table as tbl

#: Rows of each sheet an update run is shown.
DESCRIBE_ROWS = 400
_NUMBER = re.compile(r"-?\d+(\.\d+)?")


@lru_cache(maxsize=4)
def _sheets(data: bytes, kind: str) -> list[dict[str, Any]]:
    try:
        return tbl.read_table(data, kind)
    except tbl.TableError as exc:
        raise PartError(str(exc)) from exc


def _text(cell: dict[str, Any]) -> str:
    value = cell.get("v")
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _locate(data: bytes, kind: str, ident: str) -> tuple[dict[str, Any], int, int]:
    """The sheet and 0-based ``(row, col)`` of a part id."""
    sheets = _sheets(data, kind)
    name, _, ref = ident.rpartition("!")
    name = name.strip().strip("'")
    sheet = next((s for s in sheets if s["name"] == name), None) if name else sheets[0]
    if sheet is None:
        raise PartError(f"unknown sheet: {name}")
    try:
        row, col, last_row, last_col = tbl.parse_range(ref)
    except tbl.TableError as exc:
        raise PartError(str(exc)) from exc
    if (row, col) != (last_row, last_col):
        raise PartError(f"one cell at a time, not a range: {ident}")
    return sheet, row, col


def _cell(sheet: dict[str, Any], row: int, col: int) -> dict[str, Any]:
    rows = sheet["rows"]
    return rows[row][col] if row < len(rows) and col < len(rows[row]) else {}


def _number(text: str) -> float | None:
    plain = text.strip().replace(",", "")
    return float(plain) if _NUMBER.fullmatch(plain) else None


def _same(a: str, b: str) -> bool:
    if " ".join(a.split()) == " ".join(b.split()):
        return True
    x, y = _number(a), _number(b)
    return x is not None and x == y


@lru_cache(maxsize=4)
def _csv_text(data: bytes) -> list[list[str]]:
    return tbl._csv_rows(tbl._decode(data)[0])[0]


def _editor(kind: str) -> PartEditor:
    def read(data: bytes, ident: str) -> str | None:
        sheet, row, col = _locate(data, kind, ident)
        # A cell past the last row is empty, not missing: a new row is written there.
        if kind == "csv":  # as written in the file ("12.50", not 12.5)
            rows = _csv_text(data)
            return rows[row][col] if row < len(rows) and col < len(rows[row]) else ""
        return _text(_cell(sheet, row, col))

    def write(data: bytes, ident: str, text: str) -> bytes:
        sheet, row, col = _locate(data, kind, ident)
        if _cell(sheet, row, col).get("f"):
            raise PartError(f"{ident} holds a formula")
        number = _number(text) if kind == "xlsx" else None
        value: str | int | float = text
        if number is not None:
            value = int(number) if number.is_integer() and "." not in text else number
        try:
            return tbl.apply_edits(data, kind, [tbl.CellEdit(sheet["name"], row, col, value)])[0]
        except tbl.TableError as exc:
            raise PartError(str(exc)) from exc

    def describe(data: bytes) -> str:
        if kind == "csv":
            return "\n".join(
                " | ".join(f"{tbl.cell_ref(r, c)}: {raw}" for c, raw in enumerate(row) if raw)
                for r, row in enumerate(_csv_text(data)[:DESCRIBE_ROWS])
                if any(row)
            )
        sheets = _sheets(data, kind)
        many = len(sheets) > 1
        out: list[str] = []
        for sheet in sheets:
            prefix = f"{sheet['name']}!" if many else ""
            for r, row in enumerate(sheet["rows"][:DESCRIBE_ROWS]):
                cells = [
                    f"{prefix}{tbl.cell_ref(r, c)}: {cell.get('f') or _text(cell)}"
                    for c, cell in enumerate(row)
                    if cell.get("v") is not None or cell.get("f")
                ]
                if cells:
                    out.append(" | ".join(cells))
            if sheet["n_rows"] > DESCRIBE_ROWS:
                out.append(f"({sheet['n_rows'] - DESCRIBE_ROWS} more rows not shown)")
        return "\n".join(out)

    return PartEditor(
        read=read,
        write=write,
        describe=describe,
        ids=(
            "the cell holding the fact, like B3 (Sheet2!B3 when the workbook has several "
            "sheets); a new row goes in the first empty row; never a cell that starts with ="
        ),
        same=_same,
    )


for _kind in tbl.TABLE_KINDS:
    PART_EDITORS[_kind] = _editor(_kind)
