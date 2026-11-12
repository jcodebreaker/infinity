import { useSyncExternalStore } from "react";
import { resetEditorState } from "../artifacts/editor/editor-state";

// Deck UI state shared between the deck's header controls (Theme, Present, rendered in the editor
// shell's actions slot) and the deck's stage. Keyed by `artifactKey` so a new version of the same
// deck keeps its state. Edit mode itself is the shell's (`artifacts/editor/editor-state`).

type DeckUi = { presentRequest: number; themeOpen: boolean };
const NONE: DeckUi = { presentRequest: 0, themeOpen: false };
const states = new Map<string, DeckUi>();
const listeners = new Set<() => void>();

const read = (key: string): DeckUi => states.get(key) ?? NONE;
function write(key: string, patch: Partial<DeckUi>): void {
  states.set(key, { ...read(key), ...patch });
  for (const listener of listeners) listener();
}
const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => void listeners.delete(listener);
};

/** Whether the Theme gallery is open (the header's Theme button toggles it; the stage shows it). */
export const useDeckThemeOpen = (key: string): boolean =>
  useSyncExternalStore(subscribe, () => read(key).themeOpen);
export const readDeckThemeOpen = (key: string): boolean => read(key).themeOpen;
export const setDeckThemeOpen = (key: string, themeOpen: boolean): void =>
  write(key, { themeOpen });

/** A counter the header bumps; the stage enters present mode when it changes. */
export const useDeckPresentRequest = (key: string): number =>
  useSyncExternalStore(subscribe, () => read(key).presentRequest);
export const requestDeckPresent = (key: string): void =>
  write(key, { presentRequest: read(key).presentRequest + 1 });

/** Test helper: the deck's state and the editor's. */
export const resetDeckUi = (): void => {
  states.clear();
  resetEditorState();
  for (const listener of listeners) listener();
};
