import type { TableCell } from "@superchat/contracts";

/**
 * Display formatting for the common Excel number formats. Display only: the formula bar and the
 * editor always show the raw value. Anything not understood returns null (the caller shows the
 * raw value).
 */

type Section = { raw: string; condition: ((n: number) => boolean) | null };

const MONTHS = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];
const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

/** Splits on `;` outside quotes, brackets and escapes. */
function splitSections(code: string): string[] {
  const out: string[] = [];
  let cur = "";
  let quoted = false;
  let bracket = false;
  for (let i = 0; i < code.length; i += 1) {
    const ch = code[i] as string;
    if (ch === "\\" && !quoted) {
      cur += ch + (code[i + 1] ?? "");
      i += 1;
      continue;
    }
    if (ch === '"') quoted = !quoted;
    else if (!quoted && ch === "[") bracket = true;
    else if (!quoted && ch === "]") bracket = false;
    if (ch === ";" && !quoted && !bracket) {
      out.push(cur);
      cur = "";
    } else cur += ch;
  }
  out.push(cur);
  return out;
}

type Token =
  | { kind: "lit"; text: string }
  | { kind: "num"; text: string } // run of # 0 ? , . (and E+/E-)
  | { kind: "pct" }
  | { kind: "date"; text: string }; // y m d h s e.g. "yyyy", "mm", "AM/PM"

const cmp: Record<string, (a: number, b: number) => boolean> = {
  "<": (a, b) => a < b,
  "<=": (a, b) => a <= b,
  ">": (a, b) => a > b,
  ">=": (a, b) => a >= b,
  "=": (a, b) => a === b,
  "<>": (a, b) => a !== b,
};

/** Tokenizes one section; `unknown` is set for constructs we do not format (fractions, text). */
function tokenize(section: string): {
  tokens: Token[];
  unknown: boolean;
  condition: Section["condition"];
} {
  const tokens: Token[] = [];
  let unknown = false;
  let condition: Section["condition"] = null;
  const push = (token: Token) => {
    const last = tokens[tokens.length - 1];
    if (last && last.kind === "lit" && token.kind === "lit") last.text += token.text;
    else tokens.push(token);
  };
  for (let i = 0; i < section.length; ) {
    const rest = section.slice(i);
    const ch = section[i] as string;
    if (ch === '"') {
      const end = section.indexOf('"', i + 1);
      const stop = end < 0 ? section.length : end;
      push({ kind: "lit", text: section.slice(i + 1, stop) });
      i = stop + 1;
    } else if (ch === "\\") {
      push({ kind: "lit", text: section[i + 1] ?? "" });
      i += 2;
    } else if (ch === "_" || ch === "*") {
      // `_x` pads the width of x; `*x` repeats x to fill: neither matters for display.
      if (ch === "_") push({ kind: "lit", text: " " });
      i += 2;
    } else if (ch === "[") {
      const end = section.indexOf("]", i);
      const body = end < 0 ? section.slice(i + 1) : section.slice(i + 1, end);
      i = end < 0 ? section.length : end + 1;
      const money = /^\$([^-\]]*)(?:-.*)?$/.exec(body);
      const test = /^(<=|>=|<>|<|>|=)\s*(-?[0-9.]+)$/.exec(body);
      if (money) push({ kind: "lit", text: money[1] as string });
      else if (test) {
        const op = cmp[test[1] as string] as (a: number, b: number) => boolean;
        const limit = Number(test[2]);
        condition = (n) => op(n, limit);
      } else if (/^(h+|m+|s+)$/i.test(body)) {
        push({ kind: "date", text: `[${body.toLowerCase()}]` });
      }
      // colors, locales and anything else in brackets: ignored
    } else if (/[#0?]/.test(ch) || (ch === "." && /^\.[#0?]/.test(rest))) {
      const m = /^(?:[#0?,.]+(?:E[+-][0#]+)?)/i.exec(rest) as RegExpExecArray;
      const prev = tokens.at(-1);
      if (prev && prev.kind === "num") unknown = true; // two digit runs: fractions, dashes in phone formats
      push({ kind: "num", text: m[0] });
      i += m[0].length;
    } else if (ch === "%") {
      push({ kind: "pct" });
      i += 1;
    } else if (/^(AM\/PM|A\/P)/i.test(rest)) {
      const m = /^(AM\/PM|A\/P)/i.exec(rest) as RegExpExecArray;
      push({ kind: "date", text: m[0] });
      i += m[0].length;
    } else if (/[ymdhs]/i.test(ch)) {
      const m = /^([ymdhs])\1*/i.exec(rest) as RegExpExecArray;
      push({ kind: "date", text: m[0].toLowerCase() });
      i += m[0].length;
    } else if (ch === "/" && tokens.at(-1)?.kind === "num") {
      unknown = true; // a fraction format (`# ?/?`)
      i += 1;
    } else if (/[@]/.test(ch) || ch === "G" || ch === "E") {
      unknown = true;
      i += 1;
    } else {
      push({ kind: "lit", text: ch });
      i += 1;
    }
  }
  return { tokens, unknown, condition };
}

const pad = (n: number, width = 2) => String(n).padStart(width, "0");

type Parts = { y: number; mo: number; d: number; h: number; mi: number; s: number; wd: number };

/** Excel serial -> calendar parts (1900 date system, with its fictitious leap day). */
export function serialToParts(serial: number): Parts | null {
  if (!Number.isFinite(serial) || serial < 0 || serial >= 2958466) return null;
  let days = Math.floor(serial);
  let secs = Math.round((serial - days) * 86400);
  if (secs >= 86400) {
    secs -= 86400;
    days += 1;
  }
  // 1900-02-29 does not exist; serials after it are shifted by one day.
  const adjusted = days > 60 ? days - 1 : days;
  const date = new Date(Date.UTC(1899, 11, 31 + adjusted));
  return {
    y: date.getUTCFullYear(),
    mo: date.getUTCMonth() + 1,
    d: date.getUTCDate(),
    h: Math.floor(secs / 3600),
    mi: Math.floor((secs % 3600) / 60),
    s: secs % 60,
    wd: date.getUTCDay(),
  };
}

/** `2024-03-01`, `2024-03-01T09:30:00` or `09:30:00` (what the engine reports for date cells). */
export function isoToParts(iso: string): Parts | null {
  const m = /^(?:(\d{4})-(\d{2})-(\d{2}))?(?:[T ]?(\d{2}):(\d{2})(?::(\d{2}))?)?/.exec(iso);
  if (!m || (!m[1] && !m[4])) return null;
  const y = Number(m[1] ?? 1900);
  const mo = Number(m[2] ?? 1);
  const d = Number(m[3] ?? 1);
  const wd = m[1] ? new Date(Date.UTC(y, mo - 1, d)).getUTCDay() : 0;
  return { y, mo, d, h: Number(m[4] ?? 0), mi: Number(m[5] ?? 0), s: Number(m[6] ?? 0), wd };
}

function formatDate(tokens: Token[], parts: Parts): string {
  const hasAmPm = tokens.some((t) => t.kind === "date" && /^(am\/pm|a\/p)$/i.test(t.text));
  let out = "";
  tokens.forEach((token, index) => {
    if (token.kind === "lit") {
      out += token.text;
      return;
    }
    if (token.kind !== "date") return;
    const text = token.text;
    const lower = text.toLowerCase();
    if (/^(am\/pm|a\/p)$/.test(lower)) {
      const am = parts.h < 12;
      out += lower === "a/p" ? (am ? "a" : "p") : am ? "AM" : "PM";
    } else if (lower[0] === "y") out += lower.length <= 2 ? pad(parts.y % 100) : pad(parts.y, 4);
    else if (lower[0] === "d") {
      out +=
        lower.length === 1
          ? String(parts.d)
          : lower.length === 2
            ? pad(parts.d)
            : lower.length === 3
              ? (DAYS[parts.wd] as string).slice(0, 3)
              : (DAYS[parts.wd] as string);
    } else if (lower[0] === "h") {
      const h = hasAmPm ? parts.h % 12 || 12 : parts.h;
      out += lower.length === 1 ? String(h) : pad(h);
    } else if (lower[0] === "s") out += lower.length === 1 ? String(parts.s) : pad(parts.s);
    else if (lower[0] === "m") {
      // `m` after an hour or before a second is minutes, otherwise it is the month.
      const before = tokens
        .slice(0, index)
        .reverse()
        .find((t) => t.kind === "date");
      const after = tokens.slice(index + 1).find((t) => t.kind === "date");
      const minutes =
        (before?.kind === "date" && /^h/i.test(before.text)) ||
        (after?.kind === "date" && /^s/i.test(after.text));
      if (minutes) out += lower.length === 1 ? String(parts.mi) : pad(parts.mi);
      else if (lower.length === 1) out += String(parts.mo);
      else if (lower.length === 2) out += pad(parts.mo);
      else if (lower.length === 3) out += (MONTHS[parts.mo - 1] as string).slice(0, 3);
      else if (lower.length === 4) out += MONTHS[parts.mo - 1] as string;
      else out += (MONTHS[parts.mo - 1] as string).slice(0, 1);
    } else if (lower.startsWith("[")) {
      out += String(parts.h);
    }
  });
  return out;
}

function groupThousands(digits: string): string {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function formatNumber(tokens: Token[], value: number): string | null {
  const run = tokens.find((t): t is Extract<Token, { kind: "num" }> => t.kind === "num");
  if (!run) {
    // a text-only section, e.g. `"zero"` for a zero value
    return tokens.map((t) => (t.kind === "lit" ? t.text : t.kind === "pct" ? "%" : "")).join("");
  }
  const percent = tokens.some((t) => t.kind === "pct");
  const spec = run.text;
  const sci = /E[+-]/i.exec(spec);
  const mantissaSpec = sci ? spec.slice(0, sci.index) : spec;
  const dot = mantissaSpec.indexOf(".");
  const intSpec = dot < 0 ? mantissaSpec : mantissaSpec.slice(0, dot);
  const fracSpec = dot < 0 ? "" : mantissaSpec.slice(dot + 1);
  const decimals = (fracSpec.match(/[0#?]/g) ?? []).length;
  const minDecimals = (fracSpec.match(/0/g) ?? []).length;
  const minInt = (intSpec.match(/0/g) ?? []).length;
  const grouped = /[0#?],[0#?]/.test(intSpec);
  const scale = intSpec.match(/,+$/)?.[0].length ?? 0; // trailing commas divide by 1000 each
  const negative = value < 0;
  let n = Math.abs(value);
  if (percent) n *= 100;
  n /= 1000 ** scale;

  let body: string;
  if (sci) {
    const exp = n === 0 ? 0 : Math.floor(Math.log10(n));
    const mant = n / 10 ** exp;
    const expDigits = (spec.slice(sci.index + 2).match(/[0#]/g) ?? []).length;
    const sign = exp < 0 ? "-" : sci[0][1] === "+" ? "+" : "";
    body = `${mant.toFixed(decimals)}E${sign}${pad(Math.abs(exp), expDigits)}`;
  } else {
    let fixed = (Math.round((n + Number.EPSILON) * 10 ** decimals) / 10 ** decimals).toFixed(
      decimals,
    );
    let [intPart = "", fracPart = ""] = fixed.split(".");
    while (fracPart.length > minDecimals && fracPart.endsWith("0"))
      fracPart = fracPart.slice(0, -1);
    if (intPart === "0" && minInt === 0 && decimals > 0 && fracPart !== "") intPart = "";
    intPart = intPart.padStart(minInt, "0");
    if (grouped) intPart = groupThousands(intPart);
    fixed = fracPart ? `${intPart}.${fracPart}` : intPart;
    body = fixed;
  }
  const isZero = Number(body.replace(/[^0-9.]/g, "")) === 0;
  let out = "";
  for (const token of tokens) {
    if (token.kind === "lit") out += token.text;
    else if (token.kind === "num") {
      if (token === run) out += body;
    } else if (token.kind === "pct") out += "%";
  }
  return negative && !isZero ? `-${out}` : out;
}

/** Cache of parsed formats: the grid renders many cells with the same code. */
const parsed = new Map<string, Array<ReturnType<typeof tokenize>> | null>();

function sectionsOf(code: string) {
  let found = parsed.get(code);
  if (found === undefined) {
    const raw = splitSections(code);
    found = raw.length > 4 ? null : raw.map(tokenize);
    parsed.set(code, found);
  }
  return found;
}

const isDateTokens = (tokens: Token[]) =>
  tokens.some((t) => t.kind === "date") && !tokens.some((t) => t.kind === "num");

/** The cell as the file's number format shows it, or null for "show the raw value". */
export function formatCellValue(cell: TableCell): string | null {
  const code = cell.z;
  const value = cell.v;
  if (!code || value === null || value === undefined || typeof value === "boolean") return null;
  if (code === "General" || code === "@") return null;
  const sections = sectionsOf(code);
  if (!sections || sections.some((s) => s.unknown)) return null;

  const isoDate = typeof value === "string" && cell.t === "d";
  if (typeof value === "string" && !isoDate) return null;

  let section = sections[0];
  if (typeof value === "number") {
    const conditional = sections.find((s) => s.condition?.(value));
    if (conditional) section = conditional;
    else if (sections.length > 1 && !sections.some((s) => s.condition)) {
      if (value < 0) section = sections[1];
      else if (value === 0 && sections.length > 2) section = sections[2];
    }
  }
  if (!section) return null;
  const tokens = section.tokens;

  if (isDateTokens(tokens)) {
    const dateParts = isoDate ? isoToParts(value as string) : serialToParts(value as number);
    return dateParts ? formatDate(tokens, dateParts) : null;
  }
  if (typeof value !== "number") return null;
  const useAbs = sections.length > 1 && value < 0 && section === sections[1];
  return formatNumber(tokens, useAbs ? Math.abs(value) : value);
}
