// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../../test/i18n")).withI18nRoot(
    await orig<typeof import("react-dom/client")>(),
  ),
);

vi.mock("../../../lib/use-user-id", () => ({ useUserId: () => "user-1" }));
vi.mock("@lingui/react/macro", () => ({
  useLingui: () => ({ t: (parts: TemplateStringsArray) => parts.join("") }),
}));

import { FirstRunHint } from "./FirstRunHint";

let container: HTMLDivElement | null = null;

// jsdom's real `localStorage` getter is unreliable under some Node builds (falls back to
// Node's own experimental webstorage, which needs `--localstorage-file`); stub a plain
// Map-backed one instead, same shape `firstRunStorage.ts` reads and writes.
function stubLocalStorage() {
  const store = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => {
      store.set(key, value);
    },
    removeItem: (key: string) => {
      store.delete(key);
    },
    clear: () => store.clear(),
  });
}

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  stubLocalStorage();
});

afterEach(() => {
  if (container) {
    document.body.removeChild(container);
    container = null;
  }
  vi.unstubAllGlobals();
});

it("shows the hint once, over the wrapped content, and hides it for good on Got it", async () => {
  container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);

  await act(async () => {
    root.render(
      <FirstRunHint hintKey="goals-section" text="Everything Superchat is working toward.">
        <button type="button">Goals</button>
      </FirstRunHint>,
    );
  });

  expect(container.querySelector("button")?.textContent).toBe("Goals");
  const callout = container.querySelector('[data-testid="first-run-hint-goals-section"]');
  expect(callout).not.toBeNull();
  expect(callout?.textContent).toContain("Everything Superchat is working toward.");

  await act(async () => {
    container
      ?.querySelector<HTMLButtonElement>('[data-testid="first-run-hint-goals-section"] button')
      ?.click();
  });
  expect(container.querySelector('[data-testid="first-run-hint-goals-section"]')).toBeNull();
  expect(localStorage.getItem("superchat:first-run:user-1")).toBe('["goals-section"]');

  await act(async () => root.unmount());
  const root2 = createRoot(container);
  await act(async () => {
    root2.render(
      <FirstRunHint hintKey="goals-section" text="Everything Superchat is working toward.">
        <button type="button">Goals</button>
      </FirstRunHint>,
    );
  });
  expect(container.querySelector('[data-testid="first-run-hint-goals-section"]')).toBeNull();
  await act(async () => root2.unmount());
});

it("stays hidden while inactive, without marking itself seen", async () => {
  container = document.createElement("div");
  document.body.appendChild(container);
  let root = createRoot(container);

  await act(async () => {
    root.render(
      <FirstRunHint
        hintKey="feed-section"
        text="Everything Superchat has to show you."
        active={false}
      >
        <span>Feed</span>
      </FirstRunHint>,
    );
  });
  expect(container.querySelector('[data-testid="first-run-hint-feed-section"]')).toBeNull();

  await act(async () => root.unmount());
  root = createRoot(container);
  await act(async () => {
    root.render(
      <FirstRunHint hintKey="feed-section" text="Everything Superchat has to show you." active>
        <span>Feed</span>
      </FirstRunHint>,
    );
  });
  expect(container.querySelector('[data-testid="first-run-hint-feed-section"]')).not.toBeNull();
});
