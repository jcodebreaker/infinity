import type { TableCell, TableCellEdit, TableSheet } from "@superchat/contracts";

import { formatCellValue } from "./number-format";

export type CellPos = { r: number; c: number };
export type CellRange = { r0: number; c0: number; r1: number; c1: number };

/** 0-based column index -> `A`, `B` ... `AA`. */
export function colLetter(index: number): string {
  let out = "";
  let n = index + 1;
  while (n > 0) {
    const rem = (n - 1) % 26;
    out = String.fromCharCode(65 + rem) + out;
    n = Math.floor((n - 1) / 26);
  }
  return out;
}

export const cellRef = (r: number, c: number): string => `${colLetter(c)}${r + 1}`;

export function normalizeRange(a: CellPos, b: CellPos): CellRange {
  return {
    r0: Math.min(a.r, b.r),
    c0: Math.min(a.c, b.c),
    r1: Math.max(a.r, b.r),
    c1: Math.max(a.c, b.c),
  };
}

/** `C2` for one cell, `C2:C8` for a range. */
export function rangeLabel(range: CellRange): string {
  const from = cellRef(range.r0, range.c0);
  return range.r0 === range.r1 && range.c0 === range.c1
    ? from
    : `${from}:${cellRef(range.r1, range.c1)}`;
}

/** `C2:C8` or `C2` -> a range, or null when it is not one. */
export function parseRange(text: string): CellRange | null {
  const m = /^([A-Z]{1,3})([0-9]{1,7})(?::([A-Z]{1,3})([0-9]{1,7}))?$/.exec(text);
  if (!m) return null;
  const col = (letters: string) =>
    [...letters].reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0) - 1;
  const a = { r: Number(m[2]) - 1, c: col(m[1] ?? "") };
  const b = m[3] ? { r: Number(m[4]) - 1, c: col(m[3]) } : a;
  if (a.r < 0 || b.r < 0) return null;
  return normalizeRange(a, b);
}

/** A cell as a live update names it: `B3`, or `Sheet2!B3` (`sheet` is null without a name). */
export function parseCellId(id: string): { sheet: string | null; r: number; c: number } | null {
  const at = id.lastIndexOf("!");
  const range = parseRange(id.slice(at + 1).toUpperCase());
  if (!range || range.r0 !== range.r1 || range.c0 !== range.c1) return null;
  return {
    sheet: at > 0 ? id.slice(0, at).replace(/^'|'$/g, "") : null,
    r: range.r0,
    c: range.c0,
  };
}

export const rangeCellCount = (range: CellRange): number =>
  (range.r1 - range.r0 + 1) * (range.c1 - range.c0 + 1);

export const inRange = (range: CellRange, r: number, c: number): boolean =>
  r >= range.r0 && r <= range.r1 && c >= range.c0 && c <= range.c1;

export const cellKey = (sheet: string, r: number, c: number): string => `${sheet}\u0000${r}:${c}`;

const EMPTY: TableCell = { v: null, f: null, t: null };
export const cellAt = (sheet: TableSheet, r: number, c: number): TableCell =>
  sheet.rows[r]?.[c] ?? EMPTY;

/** The cell's value as plain text (no number format), or its formula text when it has no value. */
function rawText(cell: TableCell): { text: string; muted: boolean } {
  if (cell.v === null || cell.v === undefined) {
    return cell.f ? { text: cell.f, muted: true } : { text: "", muted: false };
  }
  if (typeof cell.v === "boolean") return { text: cell.v ? "TRUE" : "FALSE", muted: false };
  return { text: String(cell.v), muted: false };
}

/** The cell as shown in the grid: the value through its number format (raw when unknown). */
export function cellText(cell: TableCell): { text: string; muted: boolean } {
  const shown = rawText(cell);
  if (shown.muted) return shown;
  const formatted = formatCellValue(cell);
  return formatted === null ? shown : { text: formatted, muted: false };
}

/** What the formula bar and the editor start from: the formula, else the raw value. */
export const cellSource = (cell: TableCell): string => cell.f ?? rawText(cell).text;

export const isNumeric = (cell: TableCell): boolean =>
  cell.t === "n" || cell.t === "d" || (cell.t === null && typeof cell.v === "number");

/** What a draft becomes in the file: a plain number when it reads as one (no leading zeros). */
export function parseDraft(draft: string): TableCellEdit["value"] {
  if (draft === "") return null;
  if (/^-?(0|[1-9]\d*)(\.\d+)?$/.test(draft)) return Number(draft);
  return draft;
}

/** The cell a parsed edit produces, for the optimistic view. It keeps the old cell's number
 * format, and stays a date when a date cell is given date text (as the file does). */
export function cellFromValue(value: TableCellEdit["value"], previous?: TableCell): TableCell {
  if (value === null) return EMPTY;
  const z = previous?.z ? { z: previous.z } : {};
  if (typeof value === "string" && value.startsWith("="))
    return { v: null, f: value, t: null, ...z };
  if (typeof value === "string" && previous?.t === "d" && ISO_DATE.test(value)) {
    return { v: value, f: null, t: "d", ...z };
  }
  return {
    v: value,
    f: null,
    t: typeof value === "number" ? "n" : typeof value === "boolean" ? "b" : "s",
    ...z,
  };
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?$/;

/** Sum and count of the numeric cells in a selection. */
export function rangeSum(
  cellOf: (r: number, c: number) => TableCell,
  range: CellRange,
): { sum: number; numeric: number } {
  let sum = 0;
  let numeric = 0;
  for (let r = range.r0; r <= range.r1; r += 1) {
    for (let c = range.c0; c <= range.c1; c += 1) {
      const cell = cellOf(r, c);
      if (typeof cell.v === "number") {
        sum += cell.v;
        numeric += 1;
      }
    }
  }
  return { sum, numeric };
}

/** Formulas compare by text (the file keeps no cached results after an edit), values by value. */
const signature = (cell: TableCell): string => (cell.f ? `f${cell.f}` : `v${cell.v ?? ""}`);

/**
 * Cells that differ between two versions of a table, keyed like `cellKey`. `from` is the older
 * version; `after` supplies the cell as it now stands.
 */
export function diffTables(
  from: readonly TableSheet[],
  to: readonly TableSheet[],
): Array<{ sheet: string; row: number; col: number; before: TableCell; after: TableCell }> {
  const out: Array<{
    sheet: string;
    row: number;
    col: number;
    before: TableCell;
    after: TableCell;
  }> = [];
  for (const sheet of to) {
    const old = from.find((entry) => entry.name === sheet.name);
    if (!old) continue;
    const rows = Math.max(sheet.rows.length, old.rows.length);
    for (let r = 0; r < rows; r += 1) {
      const cols = Math.max(sheet.rows[r]?.length ?? 0, old.rows[r]?.length ?? 0);
      for (let c = 0; c < cols; c += 1) {
        const before = cellAt(old, r, c);
        const after = cellAt(sheet, r, c);
        if (signature(before) !== signature(after)) {
          out.push({ sheet: sheet.name, row: r, col: c, before, after });
        }
      }
    }
  }
  return out;
}

/** The edits that put the changed cells back as they were. */
export function revertEdits(changes: ReturnType<typeof diffTables>): TableCellEdit[] {
  return changes.map((change) => ({
    sheet: change.sheet,
    row: change.row,
    col: change.col,
    value: change.before.f ?? change.before.v,
  }));
}

/** The files the sheet viewer opens (the engine reads CSV and XLSX as tables). */
export const SHEET_MIME_TYPES: ReadonlySet<string> = new Set([
  "text/csv",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
]);
