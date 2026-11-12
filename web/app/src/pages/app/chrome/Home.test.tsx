// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../../test/i18n")).withI18nRoot(
    await orig<typeof import("react-dom/client")>(),
  ),
);
vi.mock("../../../features/approvals", () => ({ useAsks: () => ({ asks: [] }) }));

import { Home } from "./Home";

async function render(personName: string) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <Home
        personName={personName}
        dock={<nav data-testid="dock-slot" />}
        composer={<form data-testid="composer-slot" />}
        recent={<aside data-testid="recent-slot" />}
        onOpenSettings={() => undefined}
      />,
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

it("greets the person by first name over the composer, with the dock and the activity slot", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const page = await render("Jamie Rivera");
  try {
    expect(page.container.querySelector("h1")?.textContent).toContain("Hi Jamie");
    expect(page.container.querySelector("h1")?.textContent).toContain("build with you?");
    for (const id of ["dock-slot", "composer-slot", "recent-slot"]) {
      expect(page.container.querySelector(`[data-testid='${id}']`)).toBeTruthy();
    }
    expect(page.container.querySelector("button[aria-label='Settings']")).toBeTruthy();
  } finally {
    await page.cleanup();
    vi.unstubAllGlobals();
  }
});

it("greets without a name when there is none", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const page = await render("");
  try {
    expect(page.container.querySelector("h1")?.textContent).toMatch(/^What can superchat/);
  } finally {
    await page.cleanup();
    vi.unstubAllGlobals();
  }
});
