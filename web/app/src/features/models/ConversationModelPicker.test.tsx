// @vitest-environment jsdom

import type { ReactNode } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../test/i18n")).withI18nRoot(await orig<typeof import("react-dom/client")>()),
);
vi.mock("@lingui/react/macro", () => {
  const t = (parts: TemplateStringsArray, ...values: unknown[]) =>
    parts.reduce((acc, part, i) => `${acc}${part}${values[i] ?? ""}`, "");
  return {
    useLingui: () => ({ t, i18n: { locale: "en" } }),
    Trans: ({ children }: { children: ReactNode }) => children,
  };
});

const state = vi.hoisted(() => ({
  status: { enabled: true } as { enabled: boolean } | null,
  sessionModel: vi.fn(),
  setSessionModel: vi.fn(),
}));
vi.mock("./engine-models", () => ({
  useEngineModelsStatus: () => [state.status, () => undefined, true],
}));
vi.mock("../../lib/rpc", () => ({
  rpc: {
    engineModels: { sessionModel: state.sessionModel, setSessionModel: state.setSessionModel },
  },
}));
vi.mock("../../components/ai/orb", () => ({ SuperchatOrb: () => null, useOrbHome: () => false }));

import { ConversationModelPicker } from "./ConversationModelPicker";

const catalog = {
  harness: "pi",
  status: "ready",
  providerLabel: "Anthropic",
  defaultModel: "haiku-4",
  foundationModel: "sonnet-5",
  builderModel: "sonnet-5",
  lightModel: "haiku-4",
  picks: { foundation: "sonnet-5", builder: null, light: null },
  automatic: { foundation: "sonnet-5", builder: "sonnet-5", light: "haiku-4" },
  error: null,
  models: [
    {
      id: "sonnet-5",
      label: "Sonnet 5",
      family: "sonnet",
      isDefault: false,
    },
    { id: "haiku-4", label: "Haiku 4", family: "haiku", isDefault: true },
  ],
};

async function mount() {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const render = async () => {
    await act(async () => root.render(<ConversationModelPicker botId="bot-1" />));
    await act(async () => {});
  };
  await render();
  const trigger = () =>
    container.querySelector('[data-testid="conversation-model-trigger"]') as HTMLElement | null;
  return {
    container,
    trigger,
    pinned: () => container.querySelector('[data-testid="conversation-model-pinned"]'),
    /** The person's models changed somewhere (Settings > Models): the status is asked again. */
    statusRefetched: async () => {
      state.status = { enabled: true };
      await render();
    },
    open: async () => {
      const button = trigger() as HTMLElement;
      await act(async () => {
        button.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, button: 0 }));
        button.click();
      });
      await act(async () => {});
      return document.querySelector('[data-testid="conversation-model-menu"]') as HTMLElement;
    },
    cleanup: async () => {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}

function item(menu: HTMLElement, text: string): HTMLElement {
  return [...menu.querySelectorAll('[role="menuitemradio"]')].find((el) =>
    el.textContent?.includes(text),
  ) as HTMLElement;
}

it("is absent without the engine", async () => {
  state.status = { enabled: false };
  const view = await mount();
  try {
    expect(view.trigger()).toBeNull();
  } finally {
    state.status = { enabled: true };
    await view.cleanup();
  }
});

it("names the model the engine runs and pins the one chosen", async () => {
  state.sessionModel.mockResolvedValue({
    harness: "pi",
    model: null,
    effectiveModel: "sonnet-5",
    catalog,
  });
  state.setSessionModel.mockResolvedValue({ ok: true });
  const view = await mount();
  try {
    expect(state.sessionModel).toHaveBeenCalledWith({ botId: "bot-1" });
    // The engine's model in use (the Foundation), not the connection's default; no marker.
    expect(view.trigger()?.textContent).toBe("Sonnet 5");
    expect(view.pinned()).toBeNull();
    const menu = await view.open();
    expect(menu.textContent).toContain("Default · Sonnet 5");
    state.sessionModel.mockResolvedValue({
      harness: "pi",
      model: "haiku-4",
      effectiveModel: "haiku-4",
      catalog,
    });
    await act(async () => item(menu, "Haiku 4").click());
    expect(state.setSessionModel).toHaveBeenCalledWith({ botId: "bot-1", model: "haiku-4" });
    expect(view.trigger()?.textContent).toBe("Haiku 4pinned");
  } finally {
    await view.cleanup();
  }
});

it("follows the pin, then the Foundation once Default clears it", async () => {
  state.sessionModel.mockResolvedValue({
    harness: "pi",
    model: "haiku-4",
    effectiveModel: "haiku-4",
    catalog,
  });
  state.setSessionModel.mockResolvedValue({ ok: true });
  const view = await mount();
  try {
    expect(view.trigger()?.textContent).toContain("Haiku 4");
    expect(view.pinned()?.textContent).toBe("pinned");
    const menu = await view.open();
    state.sessionModel.mockResolvedValue({
      harness: "pi",
      model: null,
      effectiveModel: "sonnet-5",
      catalog,
    });
    await act(async () => item(menu, "Default · Sonnet 5").click());
    expect(state.setSessionModel).toHaveBeenLastCalledWith({ botId: "bot-1", model: null });
    expect(view.trigger()?.textContent).toBe("Sonnet 5");
    expect(view.pinned()).toBeNull();
  } finally {
    await view.cleanup();
  }
});

it("marks a stored model as pinned, even when it is the Foundation, and none without one", async () => {
  // The chat stores Sonnet, which is also the Foundation: it is still the chat's own pick.
  state.sessionModel.mockResolvedValue({
    harness: "pi",
    model: "sonnet-5",
    effectiveModel: "sonnet-5",
    catalog,
  });
  const view = await mount();
  try {
    expect(view.pinned()).not.toBeNull();
    // Settings made Haiku the Foundation and the chat stores nothing: it follows, unmarked.
    state.sessionModel.mockResolvedValue({
      harness: "pi",
      model: null,
      effectiveModel: "haiku-4",
      catalog: { ...catalog, foundationModel: "haiku-4" },
    });
    await view.statusRefetched();
    expect(view.trigger()?.textContent).toBe("Haiku 4");
    expect(view.pinned()).toBeNull();
  } finally {
    await view.cleanup();
  }
});

it("shows the engine's model in use, never one it works out itself", async () => {
  // The engine runs the connection's default (it resolved no Foundation): the header says so.
  state.sessionModel.mockResolvedValue({
    harness: "pi",
    model: null,
    effectiveModel: null,
    catalog,
  });
  const view = await mount();
  try {
    expect(view.trigger()?.textContent).toBe("Haiku 4");
    expect(view.pinned()).toBeNull();
  } finally {
    await view.cleanup();
  }
});
