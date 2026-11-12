// Parses the Memory Profile's plain-text block (`rpc.memory.profile`, web/packages/contracts/src/
// rpc.ts) for the context panel's Memory tab. The engine wraps it in bracketed framing lines
// ("[Standing memory about the user — provided by the system, not a message from the user]",
// "[End of standing memory]") that are a note to the model, not content for a person to read,
// so they're dropped; everything else is a "Heading:" line followed by its "- " bullet items.

export interface MemoryProfileSection {
  /** "" for stray lines with no heading above them (kept, never dropped silently). */
  heading: string;
  items: string[];
}

function isFramingLine(line: string): boolean {
  const trimmed = line.trim();
  return trimmed.startsWith("[") && trimmed.endsWith("]");
}

function isHeading(trimmedLine: string): boolean {
  return trimmedLine.length > 0 && !trimmedLine.startsWith("-") && trimmedLine.endsWith(":");
}

export function parseMemoryProfile(raw: string | null | undefined): MemoryProfileSection[] {
  if (!raw) return [];
  const sections: MemoryProfileSection[] = [];
  let current: MemoryProfileSection | null = null;

  for (const line of raw.split("\n")) {
    if (isFramingLine(line)) continue;
    const trimmed = line.trim();
    if (!trimmed) continue;

    if (isHeading(trimmed)) {
      current = { heading: trimmed.slice(0, -1).trim(), items: [] };
      sections.push(current);
      continue;
    }

    const item = (trimmed.startsWith("-") ? trimmed.slice(1) : trimmed).trim();
    if (!item) continue;
    if (!current) {
      current = { heading: "", items: [] };
      sections.push(current);
    }
    current.items.push(item);
  }

  return sections.filter((section) => section.items.length > 0);
}

const IRREGULAR_VERB: Record<string, string> = {
  is: "are",
  was: "were",
  has: "have",
  does: "do",
  goes: "go",
  isn: "aren",
  doesn: "don",
  hasn: "haven",
  wasn: "weren",
};

function secondPersonVerb(verb: string): string {
  const lower = verb.toLowerCase();
  const irregular = IRREGULAR_VERB[lower];
  if (irregular) return irregular;
  if (/(ch|sh|ss|x|z)es$/.test(lower)) return lower.slice(0, -2);
  if (/[^aeiou]ies$/.test(lower)) return `${lower.slice(0, -3)}y`;
  if (lower.endsWith("s") && !lower.endsWith("ss")) return lower.slice(0, -1);
  return lower;
}

const USER_POSSESSIVE = /^(?:the\s+)?user[’']s\s+/i;
const USER_SUBJECT = /^(?:the\s+)?user\s+([A-Za-z]+)(?:([’']t)\b)?/i;

/**
 * The Memory Profile is written about the person in the third person ("The user's
 * favourite fruit is mango."). The person reads it, so show it to them in second person.
 * A deterministic, display-only rewrite of the common leading "The user …" / "User …"
 * forms; anything else, and the stored data, is left untouched.
 */
export function addressMemoryItem(item: string): string {
  if (USER_POSSESSIVE.test(item)) return item.replace(USER_POSSESSIVE, "Your ");
  const match = USER_SUBJECT.exec(item);
  if (!match) return addressMidSentence(item);
  const [whole, verb = "", contraction] = match;
  const converted = secondPersonVerb(verb);
  const rest = item.slice(whole.length);
  let result = `You ${converted}${contraction ?? ""}${rest}`;
  // Fix "and is" (or similar third-person verbs) after "You are" → "and are"
  if (converted === "are") {
    result = result.replace(/and is\b/gi, "and are");
    result = result.replace(/and has\b/gi, "and have");
    result = result.replace(/and does\b/gi, "and do");
    result = result.replace(/and was\b/gi, "and were");
  }
  return result;
}

/** "This month the user is focused on X" → "This month you're focused on X". */
function addressMidSentence(item: string): string {
  return item
    .replace(/\bthe user's\b/gi, "your")
    .replace(/\bthe user is\b/gi, "you're")
    .replace(/\bthe user has\b/gi, "you have")
    .replace(/\bthe user was\b/gi, "you were")
    .replace(/\bthe user\b/gi, "you");
}

// ---- Short display labels (display-only: the stored text is never touched) ----

const MODALS = new Set([
  "will",
  "would",
  "can",
  "could",
  "should",
  "must",
  "may",
  "might",
  "shall",
]);
const SUBJECT_ANY = /^(?:you|the user|the person|user|person)\b\s*/i;
const POSSESSIVE_ANY = /^(?:your|the user[’']s|user[’']s|the person[’']s|person[’']s)\s+/i;

const capitalize = (text: string) => (text ? text.charAt(0).toUpperCase() + text.slice(1) : text);

function thirdPerson(verb: string): string {
  const lower = verb.toLowerCase();
  if (MODALS.has(lower) || lower.endsWith("s") || lower.endsWith("ed")) return lower;
  if (/(ch|sh|ss|x|z|o)$/.test(lower)) return `${lower}es`;
  if (/[^aeiou]y$/.test(lower)) return `${lower.slice(0, -1)}ies`;
  return `${lower}s`;
}

const tidy = (text: string) =>
  text
    .replace(/\s+/g, " ")
    .replace(/[\s.;]+$/, "")
    .trim();

/**
 * A short, scannable label for one memory item: drops the leading "You / The user / The
 * person" subject, turns "Your X is Y" into "X: Y", trims the trailing period. Never invents
 * words; anything it doesn't recognise falls back to the second-person text, only tidied.
 */
export function memoryLabel(item: string): string {
  const text = tidy(item);
  if (!text) return "";

  const possessive = POSSESSIVE_ANY.exec(text);
  if (possessive) {
    const rest = text.slice(possessive[0].length);
    const match = /^(.{1,40}?)\s+(?:is|are)\s+(.+)$/i.exec(rest);
    if (match && (match[1] ?? "").split(" ").length <= 4) {
      return `${capitalize(match[1] ?? "")}: ${match[2]}`;
    }
    return capitalize(rest);
  }

  const subject = SUBJECT_ANY.exec(text);
  if (!subject || /^(?:users?|persons?)name\b/i.test(text)) return tidy(addressMemoryItem(text));
  const isYou = /^you/i.test(subject[0]);
  const rest = text.slice(subject[0].length);

  const be = /^(?:(?:are|is|am|was|were)\s+|[’']re\s+)(?:currently\s+)?(.+)$/i.exec(rest);
  if (be) return capitalize((be[1] ?? "").replace(/^(?:an?|the)\s+(?=\S+\s)/i, ""));

  const negated = /^(?:don|doesn)[’']t\s+(.+)$/i.exec(rest);
  if (negated) return `Doesn't ${negated[1]}`;

  const verb = /^([A-Za-z]+)\s+(.+)$/.exec(rest);
  if (verb) {
    const converted = /^(?:have|has)$/i.test(verb[1] ?? "")
      ? "has"
      : isYou
        ? thirdPerson(verb[1] ?? "")
        : (verb[1] ?? "").toLowerCase();
    return capitalize(`${converted} ${verb[2]}`);
  }
  return capitalize(rest) || text;
}

/** "Needs to resolve whether …" → "Resolve whether …" (an open question reads as the task). */
export function questionLabel(item: string): string {
  const label = memoryLabel(item);
  return capitalize(label.replace(/^needs?\s+to\s+/i, ""));
}

/** A claim that is an open question rather than something settled. */
export function isOpenQuestion(item: string): boolean {
  const text = item.trim();
  return (
    text.endsWith("?") ||
    /\b(?:needs? to (?:resolve|decide|figure out|confirm|determine)|open question|unresolved|still deciding|yet to be decided|to be decided|tbd)\b/i.test(
      text,
    )
  );
}

/** A commitment reads as the action: "Will send X" → "Send X", "Promised to review X" → "Review X". */
export function commitmentLabel(item: string): string {
  return capitalize(
    memoryLabel(item).replace(
      /^(?:will|would|promised to|promises to|plans to|needs to|wants to)\s+/i,
      "",
    ),
  );
}

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DUE_DATE =
  /(?:\s*\b(?:by|on|before|due|until)\s+)?\b(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.?,?\s+)?(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4}))?/i;

export interface DueDate {
  /** "Wed, Oct 8". */
  label: string;
  overdue: boolean;
}

/** Pulls a calendar date ("by Wed, Oct 8", "due Oct 1") out of a commitment's text. */
export function parseDue(
  text: string,
  now: Date = new Date(),
): { text: string; due: DueDate | null } {
  const match = DUE_DATE.exec(text);
  if (!match) return { text, due: null };
  const month = MONTHS.findIndex((m) => m.toLowerCase() === (match[1] ?? "").toLowerCase());
  const day = Number(match[2]);
  const year = match[3] ? Number(match[3]) : now.getFullYear();
  const date = new Date(year, month, day);
  if (date.getMonth() !== month) return { text, due: null };
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const stripped = `${text.slice(0, match.index)}${text.slice(match.index + match[0].length)}`
    .replace(/\s+([.,;])/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
  return {
    text: stripped,
    due: {
      label: `${WEEKDAYS[date.getDay()]}, ${MONTHS[month]} ${day}`,
      overdue: date.getTime() < today.getTime(),
    },
  };
}

export interface PersonMemory {
  name: string;
  relation: string | null;
  facts: string | null;
}

/** "Dana Okafor is your manager; owns the Q3 review." → name, relation, one line of facts. */
export function parsePerson(item: string): PersonMemory {
  const text = tidy(addressMemoryItem(item));
  const isName = (name: string) => name.length > 0 && name.split(" ").length <= 4;
  const relational =
    /^(.+?)\s+(?:is|are)\s+(?:your|the user[’']s)\s+([^;,—–]+?)\s*(?:[;,—–]\s*(.+))?$/i.exec(text);
  if (relational && isName(relational[1] ?? "")) {
    return {
      name: relational[1] ?? "",
      relation: relational[2] ?? null,
      facts: relational[3]
        ? capitalize(relational[3].replace(/^she\s+|^he\s+|^they\s+/i, ""))
        : null,
    };
  }
  const bracketed = /^(.+?)\s*\(([^)]+)\)\s*[:—–,;]?\s*(.*)$/.exec(text);
  if (bracketed && isName(bracketed[1] ?? "")) {
    return {
      name: bracketed[1] ?? "",
      relation: bracketed[2] ?? null,
      facts: capitalize(bracketed[3] ?? "") || null,
    };
  }
  const dashed = /^(.+?)\s*[:—–]\s*(.+)$/.exec(text);
  if (dashed && isName(dashed[1] ?? "")) {
    return { name: dashed[1] ?? "", relation: null, facts: capitalize(dashed[2] ?? "") };
  }
  return { name: text, relation: null, facts: null };
}
