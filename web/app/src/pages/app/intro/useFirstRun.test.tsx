// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

vi.mock("../../../lib/use-user-id", () => ({ useUserId: () => "user-1" }));

import { useFirstRun } from "./useFirstRun";

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

function Harness({ hintKey }: { hintKey: string }) {
  const { seen, markSeen } = useFirstRun(hintKey);
  return (
    <div>
      <span data-testid="seen">{seen ? "seen" : "unseen"}</span>
      <button type="button" onClick={markSeen}>
        mark seen
      </button>
    </div>
  );
}

it("starts unseen and persists across a remount", async () => {
  container = document.createElement("div");
  document.body.appendChild(container);
  let root = createRoot(container);

  await act(async () => root.render(<Harness hintKey="welcome" />));
  expect(container.querySelector("[data-testid='seen']")?.textContent).toBe("unseen");

  await act(async () => {
    container?.querySelector("button")?.click();
  });
  expect(container.querySelector("[data-testid='seen']")?.textContent).toBe("seen");
  expect(localStorage.getItem("superchat:first-run:user-1")).toBe('["welcome"]');

  await act(async () => root.unmount());
  root = createRoot(container);
  await act(async () => root.render(<Harness hintKey="welcome" />));
  expect(container.querySelector("[data-testid='seen']")?.textContent).toBe("seen");
});

it("keeps flags independent per key", async () => {
  container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);

  await act(async () => root.render(<Harness hintKey="goals-section" />));
  await act(async () => {
    container?.querySelector("button")?.click();
  });
  expect(container.querySelector("[data-testid='seen']")?.textContent).toBe("seen");

  await act(async () => root.render(<Harness hintKey="feed-section" />));
  expect(container.querySelector("[data-testid='seen']")?.textContent).toBe("unseen");
});
