import type { Artifact } from "@superchat/contracts";
import { useCallback, useSyncExternalStore } from "react";
import { watchFamily } from "../../lib/family-stream";
import { rpc } from "../../lib/rpc";

// The files Superchat and its Helpers made for a Muse (a save or a live update), newest first:
// one reader per Muse, shared by every work strip's and Round strip's tiles. Read while anything
// shows them, and again each time the engine says a file was saved.

const NONE: readonly Artifact[] = [];

type Store = {
  rows: readonly Artifact[];
  listeners: Set<() => void>;
  stop: (() => void) | null;
  generation: number;
};

const stores = new Map<string, Store>();

/** Superchat's own files from the Library rows, newest first. */
export function madeFiles(rows: readonly Artifact[]): Artifact[] {
  return rows
    .filter((row) => row.origin === "ai" || row.origin === "update")
    .sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt));
}

function storeFor(botId: string): Store {
  let store = stores.get(botId);
  if (!store) {
    store = { rows: NONE, listeners: new Set(), stop: null, generation: 0 };
    stores.set(botId, store);
  }
  return store;
}

function read(botId: string, store: Store) {
  const generation = ++store.generation;
  rpc.artifacts.list({ botId }).then(
    (rows) => {
      if (generation !== store.generation) return;
      store.rows = madeFiles(rows);
      for (const listener of [...store.listeners]) listener();
    },
    () => undefined,
  );
}

function subscribe(botId: string, listener: () => void): () => void {
  const store = storeFor(botId);
  store.listeners.add(listener);
  if (!store.stop) {
    read(botId, store);
    store.stop = watchFamily(botId, (event) => {
      if (event.type === "artifactsChanged") read(botId, store);
    });
  }
  return () => {
    store.listeners.delete(listener);
    if (store.listeners.size === 0) {
      store.stop?.();
      store.stop = null;
    }
  };
}

/** The Muse's files Superchat made, newest first (none while `active` is false). */
export function useConversationFiles(botId: string, active = true): readonly Artifact[] {
  const listen = useCallback(
    (listener: () => void) => (active ? subscribe(botId, listener) : () => undefined),
    [botId, active],
  );
  return useSyncExternalStore(listen, () => (active ? storeFor(botId).rows : NONE));
}

/** Forget every reader (tests). */
export function resetConversationFiles(): void {
  for (const store of stores.values()) store.stop?.();
  stores.clear();
}
