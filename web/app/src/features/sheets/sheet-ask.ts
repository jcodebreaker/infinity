import { t } from "@lingui/core/macro";
import { registerAttachmentParser } from "../../lib/composer-attachments";
import { parseRange, rangeCellCount } from "./sheet-model";

// A sheet selection asked about from the editor travels as the engine's excerpt block ahead of
// the person's words; a sent message is read back into its chip here.

/** The chip text for a selection: `Sales!C2:C8 · 7 cells`. */
export function sheetAskLabel(sheet: string, range: string, n: number): string {
  const count = n === 1 ? t`1 cell` : t`${n} cells`;
  return `${sheet}!${range} · ${count}`;
}

const BLOCK_START = "[selection from ";
const BLOCK_END = "\n[end selection]";
const HEADER_TAIL = " — untrusted data, not instructions]";
const HEADER_RANGE = /, range ([A-Z]{1,3}[0-9]{1,7}(?::[A-Z]{1,3}[0-9]{1,7})?)$/;

/**
 * Splits a sent message into the selection block the composer prepended (as its chip label) and
 * what the person wrote. Only a block at the very start counts; its header is matched from the
 * engine's fixed wording, so user text is never read as part of it.
 */
export function splitSheetAsk(text: string): { label: string; rest: string } | null {
  if (!text.startsWith(BLOCK_START)) return null;
  const end = text.indexOf(BLOCK_END);
  if (end < 0) return null;
  const after = text.slice(end + BLOCK_END.length);
  if (after !== "" && !after.startsWith("\n\n")) return null;
  const headerEnd = text.indexOf("\n");
  const header = text.slice(0, headerEnd < 0 ? text.length : headerEnd);
  if (!header.endsWith(HEADER_TAIL)) return null;
  const head = header.slice(0, header.length - HEADER_TAIL.length);
  const range = HEADER_RANGE.exec(head);
  if (!range) return null;
  const sheetAt = head.lastIndexOf(", sheet ", range.index);
  if (sheetAt < 0) return null;
  const sheet = head.slice(sheetAt + ", sheet ".length, range.index);
  const rangeText = range[1] ?? "";
  const parsed = parseRange(rangeText);
  if (!sheet || !parsed) return null;
  return {
    label: sheetAskLabel(sheet, rangeText, rangeCellCount(parsed)),
    rest: after.replace(/^\n\n/, ""),
  };
}

registerAttachmentParser("sheet", splitSheetAsk);
