// Portions modified from the Omnigent web client's web/src/lib/designModePrompt.ts (sanitizeField),
// which this repository no longer carries; changes: also neutralises angle brackets and double
// quotes, because the field lands inside a tagged block here, and takes a per-call limit.

/** Default clamp per field: element text and styles come from the deck's DOM (untrusted). */
export const FIELD_MAX = 200;

// Control characters and line / paragraph separators, so untrusted text cannot forge extra lines
// of the block or close its fence. \u escapes keep this source free of literal line terminators.
// biome-ignore lint/suspicious/noControlCharactersInRegex: stripping control characters is the point
const CONTROL_CHARS = /[\u0000-\u001F\u007F-\u009F\u2028\u2029]/g;

/** Strip control characters, neutralise `<`, `>` and `"`, collapse whitespace, clamp length.
 * Returns "" for nullish (callers treat "" as absent). */
export function sanitizeField(value: string | null | undefined, max = FIELD_MAX): string {
  if (typeof value !== "string") return "";
  return value
    .replace(CONTROL_CHARS, " ")
    .replace(/</g, "‹")
    .replace(/>/g, "›")
    .replace(/"/g, "'")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, max);
}
