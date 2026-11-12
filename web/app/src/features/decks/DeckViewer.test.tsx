// @vitest-environment jsdom

import { i18n } from "@lingui/core";
import { I18nProvider } from "@lingui/react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

vi.mock("@lingui/react/macro", () => ({
  useLingui: () => ({
    t: (parts: TemplateStringsArray, ...v: unknown[]) =>
      parts.reduce((out, part, i) => out + part + (v[i] ?? ""), ""),
  }),
}));

import { artifactKey } from "../artifacts/editor/editor-state";
import { DeckViewer } from "./DeckViewer";
import { FIXTURE_DECK } from "./deck-fixture";
import { readDeckThemeOpen, setDeckThemeOpen } from "./deck-ui-state";

i18n.loadAndActivate({ locale: "en", messages: {} });

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});
afterEach(() => {
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

async function mount() {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <I18nProvider i18n={i18n}>
        <DeckViewer html={FIXTURE_DECK} title="launch.deck.html" />
      </I18nProvider>,
    );
  });
  return container;
}

/** The main stage frame (the first mounted is the thumbnails'; the stage is the last). */
function stageFrame(container: HTMLElement): HTMLIFrameElement {
  const frames = container.querySelectorAll("iframe");
  return frames[frames.length - 1] as HTMLIFrameElement;
}

function spyPosts(frame: HTMLIFrameElement) {
  const post = vi.fn();
  Object.defineProperty(frame, "contentWindow", {
    value: { postMessage: post },
    configurable: true,
  });
  return post;
}

async function hear(source: unknown, data: unknown) {
  await act(async () => {
    window.dispatchEvent(new MessageEvent("message", { data, source: source as Window }));
  });
}

it("renders the deck in the sandboxed frame with the relay and a slide rail", async () => {
  const container = await mount();
  const frame = stageFrame(container);
  expect(frame.getAttribute("sandbox")).toBe("allow-scripts");
  expect(frame.getAttribute("srcdoc")).toContain("superchat:");
  expect(frame.getAttribute("srcdoc")).toContain("Launch");
  const options = container.querySelectorAll('[role="option"]');
  expect(options).toHaveLength(3);
  expect(options[0]?.getAttribute("aria-current")).toBe("true");
  expect(container.querySelector('[data-testid="deck-counter"]')?.textContent).toBe("1 / 3");
});

it("opens with the full rail of thumbnails", async () => {
  const container = await mount();
  const rail = container.querySelector('[data-testid="deck-rail"]');
  expect(rail?.getAttribute("data-collapsed")).toBeNull();
  expect(container.querySelector('button[aria-label="Hide slides"]')).not.toBeNull();
});

it("mirrors the deck's state and only trusts its own frame", async () => {
  const container = await mount();
  const frame = stageFrame(container);
  const post = spyPosts(frame);
  const state = { type: "superchat:slide-state", protocolVersion: 1, active: 1, count: 3 };
  await hear({}, state);
  expect(container.querySelector('[data-testid="deck-counter"]')?.textContent).toBe("1 / 3");
  await hear(frame.contentWindow, state);
  expect(container.querySelector('[data-testid="deck-counter"]')?.textContent).toBe("2 / 3");
  expect(container.querySelectorAll('[role="option"]')[1]?.getAttribute("aria-current")).toBe(
    "true",
  );
  expect(post).not.toHaveBeenCalled();
});

it("drives the deck from the buttons, the keys and the rail", async () => {
  const container = await mount();
  const post = spyPosts(stageFrame(container));
  const button = (label: string) =>
    container.querySelector(`button[aria-label="${label}"]`) as HTMLButtonElement;

  await act(async () => button("Next slide").click());
  expect(post).toHaveBeenLastCalledWith(
    { type: "superchat:slide", protocolVersion: 1, action: "next" },
    "*",
  );
  for (const [key, action] of [
    ["ArrowLeft", "prev"],
    ["ArrowRight", "next"],
    ["Home", "first"],
    ["End", "last"],
  ] as const) {
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
    });
    expect(post).toHaveBeenLastCalledWith(
      { type: "superchat:slide", protocolVersion: 1, action },
      "*",
    );
  }
  await act(async () =>
    (container.querySelectorAll('[role="option"]')[2] as HTMLButtonElement).click(),
  );
  expect(post).toHaveBeenLastCalledWith(
    { type: "superchat:slide", protocolVersion: 1, action: "go", index: 2 },
    "*",
  );
});

it("leaves keys alone while typing in a field", async () => {
  const container = await mount();
  const post = spyPosts(stageFrame(container));
  const input = document.createElement("input");
  document.body.append(input);
  await act(async () => {
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
  });
  expect(post).not.toHaveBeenCalled();
});

it("parks each thumbnail on its slide once its frame says it is ready", async () => {
  const container = await mount();
  const frames = Array.from(container.querySelectorAll("iframe"));
  const thumb = frames[1] as HTMLIFrameElement;
  const post = spyPosts(thumb);
  await hear(thumb.contentWindow, { type: "superchat:deck-ready", protocolVersion: 1 });
  expect(post).toHaveBeenCalledWith(
    { type: "superchat:slide", protocolVersion: 1, action: "go", index: 1 },
    "*",
  );
});

it("only mounts thumbnails near the viewport", async () => {
  const observers: Array<(entries: Array<{ isIntersecting: boolean }>) => void> = [];
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      constructor(cb: (entries: Array<{ isIntersecting: boolean }>) => void) {
        observers.push(cb);
      }
      observe() {}
      disconnect() {}
    },
  );
  const container = await mount();
  expect(container.querySelectorAll("iframe")).toHaveLength(1);
  await act(async () => observers[0]?.([{ isIntersecting: true }]));
  expect(container.querySelectorAll("iframe")).toHaveLength(2);
  vi.unstubAllGlobals();
});

it("presents full screen and exits on the fullscreen change", async () => {
  const request = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(document, "fullscreenEnabled", { value: true, configurable: true });
  Object.defineProperty(document, "fullscreenElement", { value: null, configurable: true });
  Object.defineProperty(HTMLElement.prototype, "requestFullscreen", {
    value: request,
    configurable: true,
  });
  const container = await mount();
  await act(async () =>
    (container.querySelector('button[aria-label="Present"]') as HTMLButtonElement).click(),
  );
  expect(request).toHaveBeenCalledOnce();
  const viewer = container.querySelector('[data-testid="deck-viewer"]');
  Object.defineProperty(document, "fullscreenElement", { value: viewer, configurable: true });
  await act(async () => document.dispatchEvent(new Event("fullscreenchange")));
  expect(container.querySelector('[role="listbox"]')).toBeNull();
  expect(container.querySelector('button[aria-label="Exit present mode"]')).not.toBeNull();
  Object.defineProperty(document, "fullscreenElement", { value: null, configurable: true });
  await act(async () => document.dispatchEvent(new Event("fullscreenchange")));
  expect(container.querySelector('[role="listbox"]')).not.toBeNull();
});

it("closes the Theme gallery's open state when the deck leaves the screen", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () =>
    root.render(
      <I18nProvider i18n={i18n}>
        <DeckViewer html={FIXTURE_DECK} title="gone.deck.html" />
      </I18nProvider>,
    ),
  );
  const key = artifactKey({ botId: null, name: "gone.deck.html" });
  await act(async () => setDeckThemeOpen(key, true));
  expect(readDeckThemeOpen(key)).toBe(true);
  await act(async () => root.unmount());
  expect(readDeckThemeOpen(key)).toBe(false);
});
