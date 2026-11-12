import type { KnowledgeFile, KnowledgeStatus } from "@superchat/contracts";
import { useEffect, useSyncExternalStore } from "react";
import { rpc } from "../../lib/rpc";

/** The index state of the person's files, shared by every badge on screen: one poll, however many
 * cards. It runs while something is mounted and speeds up while a file is still being read. */
const FAST_MS = 3_000;
const IDLE_MS = 20_000;
const FAILED_MS = 60_000;
/** The host is asleep: the answer is the last-known state and nothing is changing. */
const ASLEEP_MS = 60_000;
/** A card for a file the last answer didn't list asks again, but not more often than this. */
const REFRESH_FLOOR_MS = 4_000;

let snapshot: KnowledgeStatus | null = null;
let fetchedAt = 0;
let timer: ReturnType<typeof setTimeout> | undefined;
let inflight = false;
let reindexAsked = false;
const listeners = new Set<() => void>();

function emit() {
  for (const listener of listeners) listener();
}

function nextDelay(): number {
  if (snapshot?.host === "asleep") return ASLEEP_MS;
  return snapshot?.files.some((file) => file.state === "indexing") ? FAST_MS : IDLE_MS;
}

/** Keyword-only files get embedded once the person has a connection that can (their key was
 * added after the files were). Asked once per page load. */
function maybeReindex(status: KnowledgeStatus) {
  if (reindexAsked || status.host === "asleep" || !status.embeddings.available) return;
  if (!status.files.some((file) => file.state === "searchable" && file.search === "keyword"))
    return;
  reindexAsked = true;
  void rpc.knowledge.reindex({}).catch(() => {
    reindexAsked = false;
  });
}

async function poll() {
  if (inflight) return;
  inflight = true;
  let delay = FAILED_MS;
  try {
    snapshot = await rpc.knowledge.status({});
    fetchedAt = Date.now();
    maybeReindex(snapshot);
    delay = nextDelay();
    emit();
  } catch {
    // No engine, or it is restarting: keep what we had and try again later.
  } finally {
    inflight = false;
    if (listeners.size > 0) timer = setTimeout(() => void poll(), delay);
  }
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  if (listeners.size === 1) void poll();
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) {
      clearTimeout(timer);
      timer = undefined;
    }
  };
}

/** Ask for fresh state soon (a file just appeared): now, or once the floor has passed. */
export function refreshKnowledge() {
  if (listeners.size === 0 || inflight || snapshot?.host === "asleep") return;
  clearTimeout(timer);
  timer = setTimeout(() => void poll(), Math.max(0, REFRESH_FLOOR_MS - (Date.now() - fetchedAt)));
}

/** Test seam: forget everything. */
export function resetKnowledgeStatus() {
  clearTimeout(timer);
  timer = undefined;
  snapshot = null;
  fetchedAt = 0;
  inflight = false;
  reindexAsked = false;
  listeners.clear();
}

/** Whether the host is asleep, so the shown state is the last-known one and not live. */
export function useKnowledgeAsleep(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => snapshot?.host === "asleep",
    () => false,
  );
}

/** The index state of one Library file (by artifact id), or `undefined` when the engine doesn't list it (not indexable,
 * or not known yet). */
export function useKnowledgeFile(artifactId: string): KnowledgeFile | undefined {
  const status = useSyncExternalStore(
    subscribe,
    () => snapshot,
    () => null,
  );
  const file = status?.files.find((entry) => entry.artifactId === artifactId);
  const unknown = status !== null && file === undefined;
  useEffect(() => {
    if (unknown) refreshKnowledge();
  }, [unknown]);
  return file;
}
