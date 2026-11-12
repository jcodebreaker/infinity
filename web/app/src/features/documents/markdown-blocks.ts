// A markdown document as its top-level blocks, each with its span in the source, so the editor
// can show, select and replace one block without touching a byte outside it. Line-based (no
// parser): fenced code is kept whole, and headings, lists, quotes, tables and rules are told
// apart the way CommonMark starts them; anything else is a paragraph.

export type BlockType = "heading" | "paragraph" | "list" | "quote" | "code" | "table" | "rule";

export interface MdBlock {
  type: BlockType;
  /** 1–6 for a heading. */
  level?: number;
  /** Offsets into the source: the block's first character and the end of its last line. */
  start: number;
  end: number;
  source: string;
  /** A heading's text without its markers. */
  text?: string;
  /** 1-based source lines. */
  line: number;
  lastLine: number;
}

const FENCE = /^ {0,3}(`{3,}|~{3,})/;
const ATX = /^ {0,3}(#{1,6})(?:[ \t]+|$)(.*)$/;
const RULE = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const QUOTE = /^ {0,3}>/;
const LIST = /^ {0,3}(?:[-*+]|\d{1,9}[.)])(?:[ \t]|$)/;
const SETEXT = /^ {0,3}(=+|-+)[ \t]*$/;
const TABLE_RULE = /^ {0,3}\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$/;
const INDENTED = /^(?: {2,}|\t)/;

const blank = (line: string) => line.trim() === "";
/** A line that starts a new block even right under a paragraph line. */
const interrupts = (line: string) =>
  FENCE.test(line) || ATX.test(line) || RULE.test(line) || QUOTE.test(line) || LIST.test(line);

/** The document's top-level blocks, in order. */
export function splitBlocks(source: string): MdBlock[] {
  const lines = source.split("\n");
  const starts: number[] = [];
  let offset = 0;
  for (const line of lines) {
    starts.push(offset);
    offset += line.length + 1;
  }
  const lineEnd = (i: number) => (starts[i] as number) + (lines[i] as string).length;
  const blocks: MdBlock[] = [];
  const push = (type: BlockType, first: number, last: number, extra: Partial<MdBlock> = {}) => {
    const start = starts[first] as number;
    const end = lineEnd(last);
    blocks.push({
      type,
      start,
      end,
      source: source.slice(start, end),
      line: first + 1,
      lastLine: last + 1,
      ...extra,
    });
  };

  let i = 0;
  while (i < lines.length) {
    const line = lines[i] as string;
    if (blank(line)) {
      i += 1;
      continue;
    }
    const first = i;
    const fence = FENCE.exec(line);
    if (fence) {
      const mark = fence[1] as string;
      const close = new RegExp(`^ {0,3}${mark[0] === "`" ? "`" : "~"}{${mark.length},}[ \\t]*$`);
      i += 1;
      while (i < lines.length && !close.test(lines[i] as string)) i += 1;
      const last = Math.min(i, lines.length - 1);
      push("code", first, last);
      i = last + 1;
      continue;
    }
    const atx = ATX.exec(line);
    if (atx) {
      const text = (atx[2] as string).replace(/[ \t]+#+[ \t]*$|^#+[ \t]*$/, "").trim();
      push("heading", first, first, { level: (atx[1] as string).length, text });
      i += 1;
      continue;
    }
    if (RULE.test(line)) {
      push("rule", first, first);
      i += 1;
      continue;
    }
    if (QUOTE.test(line)) {
      while (i + 1 < lines.length && !blank(lines[i + 1] as string)) i += 1;
      push("quote", first, i);
      i += 1;
      continue;
    }
    if (line.includes("|") && TABLE_RULE.test(lines[i + 1] ?? "")) {
      i += 1;
      while (i + 1 < lines.length && (lines[i + 1] as string).includes("|")) i += 1;
      push("table", first, i);
      i += 1;
      continue;
    }
    if (LIST.test(line)) {
      // Items, their wrapped lines and their indented children; a blank line ends the list
      // unless what follows is indented or another item.
      let last = i;
      let j = i + 1;
      while (j < lines.length) {
        const next = lines[j] as string;
        if (blank(next)) {
          j += 1;
          continue;
        }
        const afterBlank = blank(lines[j - 1] as string);
        if (INDENTED.test(next) || LIST.test(next)) last = j;
        else if (afterBlank || FENCE.test(next) || ATX.test(next) || RULE.test(next)) break;
        else last = j; // a lazy continuation of the item's text
        j += 1;
      }
      push("list", first, last);
      i = last + 1;
      continue;
    }
    // A paragraph, or a setext heading when its text is underlined with === or ---.
    let last = i;
    let setext = 0;
    while (last + 1 < lines.length) {
      const next = lines[last + 1] as string;
      if (blank(next)) break;
      const underline = SETEXT.exec(next);
      if (underline) {
        setext = (underline[1] as string)[0] === "=" ? 1 : 2;
        break;
      }
      if (interrupts(next)) break;
      last += 1;
    }
    if (setext) {
      const text = lines
        .slice(first, last + 1)
        .map((l) => l.trim())
        .join(" ");
      push("heading", first, last + 1, { level: setext, text });
      i = last + 2;
      continue;
    }
    push("paragraph", first, last);
    i = last + 1;
  }
  return blocks;
}

/** The source with `block` replaced by `next` (its text only: the lines around stay as they are). */
export function replaceBlock(source: string, block: Pick<MdBlock, "start" | "end">, next: string) {
  return source.slice(0, block.start) + next + source.slice(block.end);
}

/** Where a block sits: the heading it falls under (or is) and its number among the blocks of its
 * type in that section (paragraph 2 under "Goals"). */
export function blockPlace(
  blocks: readonly MdBlock[],
  index: number,
): { section: string | null; n: number } {
  const block = blocks[index];
  if (!block) return { section: null, n: 0 };
  let section: string | null = null;
  let n = 0;
  for (let k = index; k >= 0; k -= 1) {
    const at = blocks[k] as MdBlock;
    if (at.type === "heading") {
      section = at.text ?? "";
      if (k !== index) break;
      return { section, n: 1 };
    }
    if (at.type === block.type) n += 1;
  }
  return { section, n };
}

/** What the inspector can turn a text block into. */
export type BlockStyle = "paragraph" | "h1" | "h2" | "h3" | "quote" | "list";

/** The style a block has now, or null when it is not plain text (code, a table, a rule, H4+). */
export function blockStyle(block: Pick<MdBlock, "type" | "level">): BlockStyle | null {
  if (block.type === "heading") {
    return block.level && block.level <= 3 ? (`h${block.level}` as BlockStyle) : null;
  }
  return block.type === "paragraph" || block.type === "quote" || block.type === "list"
    ? block.type
    : null;
}

/** A text block's source restyled: its markers swapped, its words kept. */
export function restyleBlock(block: Pick<MdBlock, "type" | "source" | "text">, to: BlockStyle) {
  const lines =
    block.type === "heading"
      ? [block.text ?? ""]
      : block.source
          .split("\n")
          .map((line) => line.replace(/^ {0,3}(?:>[ \t]?|(?:[-*+]|\d{1,9}[.)])[ \t]+)/, "").trim())
          .filter(Boolean);
  if (to === "quote") return lines.map((line) => `> ${line}`).join("\n");
  if (to === "list") return lines.map((line) => `- ${line}`).join("\n");
  if (to === "paragraph") return lines.join("\n");
  return `${"#".repeat(Number(to.slice(1)))} ${lines.join(" ")}`;
}
