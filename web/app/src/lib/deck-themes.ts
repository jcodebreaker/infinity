import type { DeckTheme } from "@superchat/contracts";
import { useEffect, useState } from "react";
import { rpc } from "./rpc";

export type DeckThemesSource = () => Promise<{ themes: DeckTheme[]; defaultTheme: string }>;
/** A theme's sample deck (`.deck.html`), for the theme preview. */
export type DeckThemeSampleSource = (themeId: string) => Promise<string>;

let themesSource: DeckThemesSource = () => rpc.decks.themes({});
let sampleSource: DeckThemeSampleSource = async (themeId) =>
  (await rpc.decks.sample({ themeId })).html;

/** One fetch of the dictionary per page load: the thumbnails are a few hundred KB. */
let cache: Promise<{ themes: DeckTheme[]; defaultTheme: string }> | null = null;
export function loadDeckThemes(source: DeckThemesSource = themesSource) {
  cache ??= source().catch((error: unknown) => {
    cache = null;
    throw error;
  });
  return cache;
}

/** One fetch per theme per page load: a sample deck never changes while the app runs. */
const samples = new Map<string, Promise<string>>();
export function loadDeckThemeSample(themeId: string): Promise<string> {
  let found = samples.get(themeId);
  if (!found) {
    found = sampleSource(themeId).catch((error: unknown) => {
      samples.delete(themeId);
      throw error;
    });
    samples.set(themeId, found);
  }
  return found;
}

/** Test helper. */
export const resetDeckThemeCache = (): void => {
  cache = null;
  samples.clear();
};

/** Dev fixtures and tests: where the dictionary and the sample decks come from. */
export function setDeckThemeSources(sources: {
  themes?: DeckThemesSource;
  sample?: DeckThemeSampleSource;
}): void {
  if (sources.themes) themesSource = sources.themes;
  if (sources.sample) sampleSource = sources.sample;
  resetDeckThemeCache();
}

/** The theme dictionary (fetched only when `enabled`), null while it loads or can't be read. */
export function useDeckThemes(enabled = true): DeckTheme[] | null {
  const [themes, setThemes] = useState<DeckTheme[] | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    loadDeckThemes().then(
      (found) => live && setThemes(found.themes),
      () => {},
    );
    return () => {
      live = false;
    };
  }, [enabled]);
  return themes;
}
