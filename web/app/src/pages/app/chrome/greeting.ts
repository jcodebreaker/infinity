/**
 * Time-of-day greeting for the empty Conversation (the old design notes
 * "Conversation"): "Good morning, {name}." — morning 05:00–11:59, afternoon
 * 12:00–17:59, evening otherwise.
 */
export function greetingForHour(hour: number): "Good morning" | "Good afternoon" | "Good evening" {
  if (hour >= 5 && hour < 12) return "Good morning";
  if (hour >= 12 && hour < 18) return "Good afternoon";
  return "Good evening";
}

/** Account names that are not a person's: what single-user mode and sign-in fall back to. */
const PLACEHOLDER_NAMES = new Set([
  "local",
  "user",
  "admin",
  "owner",
  "root",
  "me",
  "you",
  "guest",
  "anonymous",
  "default",
  "unknown",
  "test",
]);

/**
 * The person's first name to greet them with, or "" when the account name is not a real one:
 * a placeholder ("local"), a login id or address (digits, "@", "_", "."), or a word written all
 * in lowercase (a login, not how anyone writes their name). Scripts without case are kept.
 */
export function firstName(name: string): string {
  const first = name.trim().split(/\s+/)[0] ?? "";
  if (!first || PLACEHOLDER_NAMES.has(first.toLowerCase())) return "";
  if (/[\d@_.\\/]/.test(first)) return "";
  if (first === first.toLowerCase() && first !== first.toUpperCase()) return "";
  return first;
}

export function greetingLead(now: Date, name: string): string {
  const given = firstName(name);
  return `${greetingForHour(now.getHours())}${given ? `, ${given}` : ""}.`;
}
