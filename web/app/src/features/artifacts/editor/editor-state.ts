import type { Artifact } from "@superchat/contracts";
import { useSyncExternalStore } from "react";

// Editor state shared between the parts of one open artifact that do not sit in one React tree
// branch: a kind's own controls (a deck's Theme gallery) that save versions the editor's history
// should own, and the parts a capability wants marked. Keyed by `artifactKey`, so a new version
// of the same file keeps it.

/** Where one file's editor state lives: its name under its Muse. A version has its own id and the
 * web has no family id, so the name is what every version shares. */
export const artifactKey = (artifact: Pick<Artifact, "botId" | "name">): string =>
  `${artifact.botId ?? ""}:${artifact.name}`;

/** A version the person made by hand: an edit, or restoring an older one. */
export const isHandOrigin = (origin: string | undefined): boolean =>
  origin === "manual" || origin === "restore";

const listeners = new Set<() => void>();
const notify = () => {
  for (const listener of listeners) listener();
};
const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => void listeners.delete(listener);
};

// Versions saved outside the editor (a deck's Theme switch) that its undo history should own, and
// the editor's way to save what it still has staged before such a change is made.
type Head = { id: string; version: number };
const ownVersions = new Map<string, Set<number>>();
const flushers = new Map<string, () => Promise<Head>>();

/** Says the saved `version` was an edit of the person's, so Undo can step back over it. */
export function noteOwnVersion(key: string, version: number): void {
  if (!flushers.has(key)) return; // only a mounted editor keeps history; nothing piles up otherwise
  ownVersions.set(key, (ownVersions.get(key) ?? new Set()).add(version));
}
/** True once (and forgets) when `version` was noted by `noteOwnVersion`. */
export function takeOwnVersion(key: string, version: number): boolean {
  return ownVersions.get(key)?.delete(version) ?? false;
}
/** The editor registers how to save its staged changes; returns the unregister. */
export function registerEditorFlush(key: string, flush: () => Promise<Head>): () => void {
  flushers.set(key, flush);
  return () => {
    if (flushers.get(key) !== flush) return;
    flushers.delete(key);
    ownVersions.delete(key);
  };
}
/** Saves what the editor still has staged for this file; its newest saved version, or null when
 * no editor is mounted. */
export const flushEditorEdits = async (key: string): Promise<Head | null> =>
  (await flushers.get(key)?.()) ?? null;

/** Test helper. */
export function resetEditorState(): void {
  ownVersions.clear();
  flushers.clear();
  notify();
}

// Parts of a page a capability wants marked (the parts a live update changed) and the one part it
// wants brought into view. The page editor reads both; the capability sets them, so neither
// imports the other.
const marked = new Map<string, readonly string[]>();
const NONE: readonly string[] = [];
const focused = new Map<string, { id: string; n: number }>();

/** The `data-superchat-id`s to mark in the file's page. */
export const useArtifactMarked = (key: string): readonly string[] =>
  useSyncExternalStore(subscribe, () => marked.get(key) ?? NONE);
export function setArtifactMarked(key: string, ids: readonly string[]): void {
  const now = marked.get(key) ?? NONE;
  if (now.length === ids.length && now.every((id, index) => id === ids[index])) return;
  if (ids.length) marked.set(key, ids);
  else marked.delete(key);
  notify();
}

/** The part asked to be shown (selected and scrolled to); `n` makes a repeat request new. */
export const useArtifactFocus = (key: string): { id: string; n: number } | undefined =>
  useSyncExternalStore(subscribe, () => focused.get(key));
export function focusArtifactPart(key: string, id: string): void {
  focused.set(key, { id, n: (focused.get(key)?.n ?? 0) + 1 });
  notify();
}
