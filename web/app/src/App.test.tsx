// @vitest-environment jsdom

import { ORPCError } from "@orpc/server";
import type { ReactNode } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("./test/i18n")).withI18nRoot(await orig<typeof import("react-dom/client")>()),
);

vi.mock("@lingui/react/macro", () => ({
  useLingui: () => ({ t: (parts: TemplateStringsArray) => parts.join("") }),
  Trans: ({ children }: { children?: ReactNode }) => children,
}));

const bootstrap = vi.hoisted(() => ({ loadMe: vi.fn(), resetMe: vi.fn() }));
vi.mock("./lib/bootstrap", () => bootstrap);
vi.mock("./lib/rpc", () => ({ rpc: {} }));
vi.mock("./pages/Shell", () => ({ ShellPage: () => <div data-testid="shell" /> }));

import { App } from "./App";

async function renderApp(path: string) {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>,
    );
  });
  return {
    container,
    async cleanup() {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

it("opens the Shell once the engine says who the person is", async () => {
  bootstrap.loadMe.mockResolvedValue({ userId: "user-1" });
  const page = await renderApp("/app");
  try {
    expect(page.container.querySelector('[data-testid="shell"]')).toBeTruthy();
  } finally {
    await page.cleanup();
  }
});

it("sends the root to the app", async () => {
  bootstrap.loadMe.mockResolvedValue({ userId: "user-1" });
  const page = await renderApp("/");
  try {
    expect(page.container.querySelector('[data-testid="shell"]')).toBeTruthy();
  } finally {
    await page.cleanup();
  }
});

it("shows only the sign-in message when the engine answers 401", async () => {
  bootstrap.loadMe.mockRejectedValue(new ORPCError("UNAUTHORIZED"));
  const page = await renderApp("/app");
  try {
    expect(page.container.textContent).toBe(
      "You're not signed in. Open superchat through your organisation's sign-in.",
    );
    expect(page.container.querySelector("button, input, a")).toBeNull();
  } finally {
    await page.cleanup();
  }
});

it("offers Retry when the engine cannot be reached", async () => {
  bootstrap.loadMe.mockRejectedValueOnce(new Error("network down"));
  bootstrap.loadMe.mockResolvedValue({ userId: "user-1" });
  const page = await renderApp("/app");
  try {
    const retry = [...page.container.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Retry"),
    );
    expect(retry).toBeTruthy();
    await act(async () => retry?.click());
    expect(bootstrap.resetMe).toHaveBeenCalled();
    expect(page.container.querySelector('[data-testid="shell"]')).toBeTruthy();
  } finally {
    await page.cleanup();
  }
});
