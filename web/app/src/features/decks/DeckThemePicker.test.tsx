// @vitest-environment jsdom

import { i18n } from "@lingui/core";
import { I18nProvider } from "@lingui/react";
import type { DeckTheme } from "@superchat/contracts";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

vi.mock("@lingui/react/macro", () => ({
  useLingui: () => ({
    t: (parts: TemplateStringsArray, ...v: unknown[]) =>
      parts.reduce((out, part, i) => out + part + (v[i] ?? ""), ""),
  }),
}));

import { setDeckThemeSources } from "../../lib/deck-themes";
import { registerEditorFlush, takeOwnVersion } from "../artifacts/editor/editor-state";
import { DeckThemePicker, resetDeckThemeCache } from "./DeckThemePicker";
import { resetDeckUi, setDeckThemeOpen, useDeckThemeOpen } from "./deck-ui-state";

i18n.loadAndActivate({ locale: "en", messages: {} });

const THEMES: DeckTheme[] = [
  {
    id: "corporate-clean",
    name: "Corporate Clean",
    tagline: "Sober, board-ready",
    mood: "White and navy.",
    category: "professional",
    mode: "light",
    bestFor: "reports",
    preview: "data:image/webp;base64,AA==",
  },
  {
    id: "nord",
    name: "Nord",
    tagline: "Dark frost blue",
    mood: "Arctic dark slate.",
    category: "dark",
    mode: "dark",
    bestFor: "long reviews",
    preview: "data:image/webp;base64,AA==",
  },
];
const KEY = "q3.deck.html";

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  resetDeckThemeCache();
});
const roots: Root[] = [];
afterEach(() => {
  for (const root of roots.splice(0)) act(() => root.unmount());
  document.body.innerHTML = "";
  resetDeckUi();
});

async function mount(
  over: {
    editSource?: ReturnType<typeof vi.fn>;
    current?: string | null;
    onEdited?: () => void;
  } = {},
) {
  const editSource = over.editSource ?? vi.fn(async () => ({ id: "v2", version: 2 }));
  const onEdited = over.onEdited ?? vi.fn();
  const theme = over.current === undefined ? "corporate-clean" : over.current;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  roots.push(root);
  function Probe() {
    return <span data-testid="open">{String(useDeckThemeOpen(KEY))}</span>;
  }
  const themes = async () => ({ themes: THEMES, defaultTheme: "corporate-clean" });
  const render = (version: number, now: string | null = theme) =>
    act(async () =>
      root.render(
        <I18nProvider i18n={i18n}>
          <Probe />
          <button type="button" data-deck-theme-button="" data-testid="header-button" />
          <DeckThemePicker
            deckKey={KEY}
            artifact={{ id: `v${version}`, version }}
            onEdited={onEdited}
            themesSource={themes}
            themeSource={async () => now}
            editSource={editSource as never}
          />
        </I18nProvider>,
      ),
    );
  await render(1);
  return { container, editSource, onEdited, render };
}
const card = (c: HTMLElement, id: string) =>
  c.querySelector(`[data-testid="deck-theme-${id}"]`) as HTMLButtonElement;
const key = (el: Element, name: string) =>
  act(
    async () => void el.dispatchEvent(new KeyboardEvent("keydown", { key: name, bubbles: true })),
  );

it("shows every theme with its thumbnail and tagline, grouped, the current one marked", async () => {
  const { container } = await mount();
  expect(container.querySelectorAll('[role="radio"]')).toHaveLength(2);
  expect(container.querySelectorAll('[role="radiogroup"]')).toHaveLength(1);
  expect(card(container, "corporate-clean").getAttribute("aria-checked")).toBe("true");
  expect(card(container, "nord").getAttribute("aria-checked")).toBe("false");
  expect(card(container, "nord").textContent).toContain("Dark frost blue");
  expect(card(container, "nord").querySelector("img")?.getAttribute("src")).toContain("data:image");
  expect(Array.from(container.querySelectorAll("h3")).map((h) => h.textContent)).toEqual([
    "Professional",
    "Dark",
  ]);
});

it("opens with the keyboard on the current theme and moves with the arrow keys", async () => {
  const { container } = await mount({ current: "nord" });
  expect(document.activeElement).toBe(card(container, "nord"));
  // one tab stop: the focused card
  expect(card(container, "nord").tabIndex).toBe(0);
  expect(card(container, "corporate-clean").tabIndex).toBe(-1);
  await key(card(container, "nord"), "ArrowLeft");
  expect(document.activeElement).toBe(card(container, "corporate-clean"));
  expect(card(container, "corporate-clean").tabIndex).toBe(0);
  expect(card(container, "nord").tabIndex).toBe(-1);
  await key(card(container, "corporate-clean"), "End");
  expect(document.activeElement).toBe(card(container, "nord"));
  await key(card(container, "nord"), "Home");
  expect(document.activeElement).toBe(card(container, "corporate-clean"));
});

it("focuses the first card when the deck is on no theme", async () => {
  const { container } = await mount({ current: null });
  expect(document.activeElement).toBe(card(container, "corporate-clean"));
});

it("returns focus to the header's Theme button when it closes from the keyboard", async () => {
  const { container } = await mount();
  await act(async () => setDeckThemeOpen(KEY, true));
  await key(document.body, "Escape");
  await act(async () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
  expect(document.activeElement).toBe(container.querySelector('[data-testid="header-button"]'));
});

it("says upfront that a hand-made deck is on no theme and sends nothing when a card is used", async () => {
  const { container, editSource } = await mount({ current: null });
  expect(container.textContent).toContain(
    "This deck isn't on a theme. Ask superchat to restyle it.",
  );
  expect(container.querySelector('[role="radio"][aria-checked="true"]')).toBeNull();
  await act(async () => card(container, "nord").click());
  expect(editSource).not.toHaveBeenCalled();
});

it("picking a theme sends one set-theme patch on the shown version, and Undo puts the source back", async () => {
  const { container, editSource, onEdited, render } = await mount();
  await act(async () => card(container, "nord").click());
  expect(editSource).toHaveBeenCalledWith({
    artifactId: "v1",
    baseVersion: 1,
    patches: [{ kind: "set-theme", theme: "nord" }],
  });
  expect(onEdited).toHaveBeenCalledWith("v2");
  // no editor is mounted, so nothing is kept for its history
  expect(takeOwnVersion(KEY, 2)).toBe(false);
  // the panel adopts the new version; the engine says it is on Nord
  await render(2, "nord");
  expect(card(container, "nord").getAttribute("aria-checked")).toBe("true");
  expect(container.textContent).toContain("Switched to Nord");
  const undo = Array.from(container.querySelectorAll("button")).find(
    (b) => b.textContent === "Undo",
  ) as HTMLButtonElement;
  await act(async () => undo.click());
  expect(editSource).toHaveBeenLastCalledWith({
    artifactId: "v2",
    baseVersion: 2,
    patches: [{ kind: "set-theme", theme: "corporate-clean" }],
  });
});

it("hands a saved version to a mounted editor's undo history", async () => {
  const { container } = await mount();
  registerEditorFlush(KEY, async () => ({ id: "v1", version: 1 }));
  await act(async () => card(container, "nord").click());
  expect(takeOwnVersion(KEY, 2)).toBe(true);
});

it("keeps the keyboard on the card while a pick saves", async () => {
  let release: (value: { id: string; version: number }) => void = () => {};
  const slow = vi.fn(() => new Promise<{ id: string; version: number }>((r) => (release = r)));
  const { container } = await mount({ editSource: slow as never });
  card(container, "nord").focus();
  await act(async () => void card(container, "nord").click());
  expect(document.activeElement).toBe(card(container, "nord"));
  expect(card(container, "nord").getAttribute("aria-disabled")).toBe("true");
  await act(async () => release({ id: "v2", version: 2 }));
});

it("moves the keyboard into the gallery even when the deck's theme can't be read", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  roots.push(root);
  await act(async () =>
    root.render(
      <I18nProvider i18n={i18n}>
        <DeckThemePicker
          deckKey={KEY}
          artifact={{ id: "v1", version: 1 }}
          themesSource={async () => ({ themes: THEMES, defaultTheme: "corporate-clean" })}
          themeSource={async () => {
            throw new Error("down");
          }}
          editSource={vi.fn() as never}
        />
      </I18nProvider>,
    ),
  );
  expect(document.activeElement).toBe(card(container, "corporate-clean"));
});

it("saves what the editor still has staged first and builds on its version", async () => {
  const { container, editSource } = await mount();
  registerEditorFlush(KEY, async () => ({ id: "v5", version: 5 }));
  await act(async () => card(container, "nord").click());
  expect(editSource).toHaveBeenCalledWith({
    artifactId: "v5",
    baseVersion: 5,
    patches: [{ kind: "set-theme", theme: "nord" }],
  });
});

it("words a refusal for people, never the engine's text", async () => {
  const stale = vi.fn(async () => {
    throw Object.assign(new Error("refused"), {
      code: "CONFLICT",
      data: { code: "artifact_stale" },
    });
  });
  const a = await mount({ editSource: stale });
  await act(async () => card(a.container, "nord").click());
  expect(a.container.querySelector('[role="status"]')?.textContent).toBe(
    "The deck changed since you opened it. Try again.",
  );
  const inexact = vi.fn(async () => {
    throw Object.assign(new Error("refused"), { code: "CONFLICT", data: { code: "conflict" } });
  });
  const b = await mount({ editSource: inexact });
  await act(async () => card(b.container, "nord").click());
  expect(b.container.querySelector('[role="status"]')?.textContent).toContain("Ask superchat");
  expect(card(b.container, "nord").getAttribute("aria-checked")).toBe("false");
});

it("closes on Escape and on a click outside, not on a click inside", async () => {
  const { container } = await mount();
  await act(async () => setDeckThemeOpen(KEY, true));
  const open = () => container.querySelector('[data-testid="open"]')?.textContent;
  await act(async () =>
    container
      .querySelector("h3")
      ?.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true })),
  );
  expect(open()).toBe("true");
  await act(async () =>
    document.body.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true })),
  );
  expect(open()).toBe("false");
  await act(async () => setDeckThemeOpen(KEY, true));
  await key(document.body, "Escape");
  expect(open()).toBe("false");
  await act(async () => setDeckThemeOpen(KEY, true));
  await act(async () =>
    container
      .querySelector('[data-testid="deck-theme-backdrop"]')
      ?.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true })),
  );
  expect(open()).toBe("false");
});

it("previews a theme's sample deck and switches to it from the preview", async () => {
  setDeckThemeSources({
    sample: async (id) =>
      `<!doctype html><html><body><section class="slide" data-screen-label="01 ${id}"></section></body></html>`,
  });
  const { container, editSource } = await mount();
  await act(async () => setDeckThemeOpen(KEY, true));
  const preview = container.querySelector(
    '[data-testid="deck-theme-preview-nord"]',
  ) as HTMLButtonElement;
  await act(async () => preview.click());
  await act(async () => {});
  const dialog = document.querySelector("[data-deck-preview]") as HTMLElement;
  expect(dialog.textContent).toContain("Nord");
  expect(dialog.querySelector('[data-testid="deck-viewer"]')).not.toBeNull();
  // Clicking in the lightbox leaves the gallery open.
  await act(async () => {
    dialog.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
  });
  expect(container.querySelector('[data-testid="open"]')?.textContent).toBe("true");
  const use = [...dialog.querySelectorAll("button")].find((b) => b.textContent === "Use this look");
  await act(async () => use?.click());
  expect(editSource).toHaveBeenCalledExactlyOnceWith({
    artifactId: "v1",
    baseVersion: 1,
    patches: [{ kind: "set-theme", theme: "nord" }],
  });
});
