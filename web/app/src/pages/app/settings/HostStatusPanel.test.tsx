// @vitest-environment jsdom

import type { ReactNode } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../../test/i18n")).withI18nRoot(
    await orig<typeof import("react-dom/client")>(),
  ),
);
vi.mock("@lingui/react/macro", () => {
  const t = (parts: TemplateStringsArray, ...values: unknown[]) =>
    parts.reduce((acc, part, i) => `${acc}${part}${values[i] ?? ""}`, "");
  return {
    useLingui: () => ({ t, i18n: { locale: "en" } }),
    Trans: ({ children }: { children: ReactNode }) => children,
  };
});
vi.mock("../../../lib/rpc", () => ({ rpc: {} }));

import { HostStatusPanel, type HostStatusWire } from "./HostStatusPanel";

function makeWire(overrides: Partial<HostStatusWire> = {}) {
  return {
    status: async () => ({ botId: "bot-1", state: "running", runnerReady: true }),
    sessionModel: async () => ({
      harness: "claude-sdk",
      model: null,
      catalog: { defaultModel: "sonnet-5", models: [{ id: "sonnet-5", label: "Sonnet 5" }] },
    }),
    ...overrides,
  } as unknown as HostStatusWire;
}

async function render(wire: HostStatusWire) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(<HostStatusPanel botId="bot-1" wire={wire} />));
  await act(async () => {});
  return {
    container,
    done: async () => {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}

it("shows the machine's state, the harness and the model", async () => {
  const view = await render(makeWire());
  try {
    const text = view.container.textContent ?? "";
    for (const expected of ["Running", "Claude Agent SDK", "Default · Sonnet 5"]) {
      expect(text).toContain(expected);
    }
  } finally {
    await view.done();
  }
});

it("says the machine is starting while its launch is in progress", async () => {
  const view = await render(
    makeWire({
      status: async () => ({ botId: "bot-1", state: "stopped", launch: { stage: "starting" } }),
    }),
  );
  try {
    expect(view.container.textContent).toContain("Starting");
  } finally {
    await view.done();
  }
});
