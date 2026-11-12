// @vitest-environment jsdom

import { i18n } from "@lingui/core";
import { I18nProvider } from "@lingui/react";
import type { DeckTheme } from "@superchat/contracts";
import { act, useRef, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

vi.mock("@lingui/react/macro", () => ({
  useLingui: () => ({
    t: (parts: TemplateStringsArray, ...v: unknown[]) =>
      parts.reduce((out, part, i) => out + part + (v[i] ?? ""), ""),
  }),
}));

import { setDeckThemeSources } from "../../lib/deck-themes";
import { DeckThemeChoice } from "./DeckThemeChoice";

i18n.loadAndActivate({ locale: "en", messages: {} });

const theme = (id: string, name: string, category: DeckTheme["category"]): DeckTheme => ({
  id,
  name,
  tagline: `${name} tagline`,
  mood: `${name}: a long mood sentence that the tile never shows.`,
  category,
  mode: category === "dark" ? "dark" : "light",
  bestFor: "",
  preview: `data:image/webp;base64,${id}`,
});
const THEMES = [
  theme("corporate-clean", "Corporate Clean", "professional"),
  theme("editorial-serif", "Editorial Serif", "editorial"),
  theme("tokyo-night", "Tokyo Night", "dark"),
];
const OPTIONS = THEMES.map((t, i) => ({ id: `opt-${i + 1}`, label: t.name, previewId: t.id }));

const roots: Root[] = [];
beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  setDeckThemeSources({
    themes: async () => ({ themes: THEMES, defaultTheme: "corporate-clean" }),
    sample: async (id) =>
      `<!doctype html><html><body>${[1, 2, 3, 4]
        .map((n) => `<section class="slide" data-screen-label="0${n} ${id}"></section>`)
        .join("")}</body></html>`,
  });
});
afterEach(() => {
  for (const root of roots.splice(0)) act(() => root.unmount());
  document.body.innerHTML = "";
});

/** The card's answer path as the Ask card wires it: the first pick sends, then it locks. */
async function mount(answer: string | null = null) {
  const send = vi.fn();
  function Card() {
    const [chosen, setChosen] = useState<string | null>(answer);
    const sent = useRef(false);
    return (
      <DeckThemeChoice
        options={OPTIONS}
        chosen={chosen}
        locked={chosen !== null}
        onPick={(label) => {
          if (sent.current || chosen !== null) return;
          sent.current = true;
          setChosen(label);
          send(label);
        }}
      />
    );
  }
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  roots.push(root);
  await act(async () =>
    root.render(
      <I18nProvider i18n={i18n}>
        <Card />
      </I18nProvider>,
    ),
  );
  await act(async () => {});
  return { container, send };
}

const tiles = (c: HTMLElement) =>
  [...c.querySelectorAll('[data-testid="ask-previews"] li')].map(
    (li) => li.querySelector("button") as HTMLButtonElement,
  );
const dialog = () => document.querySelector("[data-deck-preview]") as HTMLElement | null;
const dialogButton = (text: string) =>
  [...(dialog()?.querySelectorAll("button") ?? [])].find((b) => b.textContent === text);

it("shows each theme's thumbnail, name and tagline, never the long mood", async () => {
  const { container } = await mount();
  expect(tiles(container)).toHaveLength(3);
  expect(container.querySelectorAll("img")).toHaveLength(3);
  expect(container.textContent).toContain("Tokyo Night tagline");
  expect(container.textContent).not.toContain("long mood sentence");
});

it("previews the whole sample deck, flips between the offered looks and answers once", async () => {
  const { container, send } = await mount();
  const preview = container.querySelector('[aria-label="Preview Editorial Serif"]') as HTMLElement;
  await act(async () => preview.click());
  await act(async () => {});
  expect(dialog()?.textContent).toContain("Editorial Serif");
  expect(dialog()?.querySelector('[data-testid="deck-viewer"]')).not.toBeNull();
  expect(dialog()?.querySelector('[data-testid="deck-counter"]')?.textContent).toContain("/ 4");

  await act(async () => dialogButton("Tokyo Night")?.click());
  await act(async () => {});
  expect(dialogButton("Tokyo Night")?.getAttribute("aria-pressed")).toBe("true");

  await act(async () => dialogButton("Use this look")?.click());
  await act(async () => {});
  expect(send).toHaveBeenCalledExactlyOnceWith("Tokyo Night");
  expect(tiles(container).every((tile) => tile.disabled)).toBe(true);
  expect(tiles(container)[2]?.getAttribute("aria-pressed")).toBe("true");
  // a second answer never goes out
  await act(async () => tiles(container)[0]?.click());
  expect(send).toHaveBeenCalledTimes(1);
});

it("an answered card still previews, but only to look", async () => {
  const { container, send } = await mount("Corporate Clean");
  const preview = container.querySelector('[aria-label="Preview Tokyo Night"]') as HTMLElement;
  await act(async () => preview.click());
  await act(async () => {});
  expect(dialogButton("Use this look")).toBeUndefined();
  await act(async () => dialogButton("Corporate Clean")?.click());
  expect(dialog()?.textContent).toContain("Current look");
  expect(send).not.toHaveBeenCalled();
});
