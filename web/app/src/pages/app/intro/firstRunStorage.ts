// Per-user first-run flags (welcome + one-time contextual hints): which of them this
// person has already seen or dismissed. No general preferences RPC exists yet for this
// (only `preferences.update` for `avatarStyle`), so this keeps them in localStorage: one
// JSON array of seen keys per user, wrapped in try/catch so a blocked or full store never breaks the app.

const STORAGE_PREFIX = "superchat:first-run:";

function storageKey(userId: string | null | undefined): string | null {
  if (!userId) return null;
  return `${STORAGE_PREFIX}${userId}`;
}

function readSeenKeys(userId: string | null | undefined): Set<string> {
  const key = storageKey(userId);
  if (!key) return new Set();
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return new Set();
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed)
      ? new Set(parsed.filter((entry): entry is string => typeof entry === "string"))
      : new Set();
  } catch {
    return new Set();
  }
}

function writeSeenKeys(userId: string | null | undefined, seen: Set<string>): void {
  const key = storageKey(userId);
  if (!key) return;
  try {
    window.localStorage.setItem(key, JSON.stringify([...seen]));
  } catch {
    // Ignore quota / private-mode failures; in-memory state still applies this session.
  }
}

/** Whether this person has already seen or dismissed the given first-run flag. */
export function isFirstRunSeen(userId: string | null | undefined, flagKey: string): boolean {
  return readSeenKeys(userId).has(flagKey);
}

/** Marks a first-run flag seen; a no-op if it already was. */
export function markFirstRunSeen(userId: string | null | undefined, flagKey: string): void {
  const seen = readSeenKeys(userId);
  if (seen.has(flagKey)) return;
  seen.add(flagKey);
  writeSeenKeys(userId, seen);
}

/** "Replay the intro" (Settings, Superchat section): clears every first-run flag for this person. */
export function resetFirstRun(userId: string | null | undefined): void {
  const key = storageKey(userId);
  if (!key) return;
  try {
    window.localStorage.removeItem(key);
  } catch {
    // Ignore; nothing to clean up if the store is unavailable.
  }
}
