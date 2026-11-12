// @vitest-environment jsdom

import type { EngineModelsStatus } from "@superchat/contracts";
import type { ReactNode } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

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

const api = vi.hoisted(() => ({
  providers: vi.fn(),
  connections: vi.fn(),
  connect: vi.fn(),
  disconnect: vi.fn(),
  catalog: vi.fn(),
  setPick: vi.fn(),
  setHarness: vi.fn(),
  budget: vi.fn(),
  setBudget: vi.fn(),
}));
vi.mock("../../lib/rpc", () => ({ rpc: { engineModels: api } }));

import { resetEngineModelsListeners } from "./engine-models";
import { ModelsPanel } from "./ModelsPanel";

afterEach(resetEngineModelsListeners);

const status: EngineModelsStatus = {
  enabled: true,
  harnesses: [{ id: "pi", label: "Pi" }],
  ready: true,
};
const connection = {
  provider: "anthropic",
  hint: "abcd",
  status: "valid",
  validatedAt: 1_792_000_000,
  label: null,
  scope: "user" as const,
};
const catalog = {
  harness: "pi",
  status: "ready",
  providerLabel: "Anthropic",
  defaultModel: "sonnet-5",
  foundationModel: "sonnet-5",
  builderModel: "sonnet-5",
  lightModel: "haiku-4",
  picks: { foundation: null, builder: null, light: null },
  automatic: { foundation: "sonnet-5", builder: "sonnet-5", light: "haiku-4" },
  error: null,
  models: [
    {
      id: "sonnet-5",
      label: "Sonnet 5",
      family: "sonnet",
      isDefault: true,
    },
    { id: "haiku-4", label: "Haiku 4", family: "haiku", isDefault: false },
  ],
};

function setValue(el: HTMLInputElement | HTMLSelectElement, value: string) {
  const proto = el instanceof HTMLSelectElement ? HTMLSelectElement : HTMLInputElement;
  Object.getOwnPropertyDescriptor(proto.prototype, "value")?.set?.call(el, value);
  el.dispatchEvent(
    new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }),
  );
}

async function mount(onChanged = vi.fn()) {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(<ModelsPanel status={status} onChanged={onChanged} />);
  });
  await act(async () => {});
  return {
    container,
    onChanged,
    /** The status as the engine answers it after a refresh. */
    rerender: async (next: EngineModelsStatus) => {
      await act(async () => root.render(<ModelsPanel status={next} onChanged={onChanged} />));
      await act(async () => {});
    },
    button: (name: string) =>
      [...container.querySelectorAll("button")].find((b) => b.textContent === name),
    cleanup: async () => {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}

function seed(connections = [connection]) {
  api.providers.mockResolvedValue([
    { provider: "anthropic", label: "Anthropic", operatorKey: false },
    { provider: "openrouter", label: "OpenRouter", operatorKey: false },
  ]);
  api.connections.mockResolvedValue(connections);
  api.catalog.mockResolvedValue(catalog);
  api.budget.mockResolvedValue({
    monthlyLimitUsd: 20,
    atLimit: "ask",
    spentMonthUsd: 5,
    orgLimitUsd: 100,
  });
}

it("lists each provider's key as masked metadata, with Replace and Remove", async () => {
  seed();
  const view = await mount();
  try {
    const anthropic = view.container.querySelector('[data-testid="model-key-anthropic"]');
    expect(anthropic?.textContent).toContain("••••abcd");
    expect(anthropic?.textContent).toContain("Checked");
    expect(anthropic?.textContent).toContain("Replace");
    expect(anthropic?.textContent).toContain("Remove");
    const openrouter = view.container.querySelector('[data-testid="model-key-openrouter"]');
    expect(openrouter?.textContent).toContain("Not connected");
    expect(view.container.textContent).not.toContain("Provided by your organization");
  } finally {
    await view.cleanup();
  }
});

it("adds a key in a password field and shows the engine's rejection inline", async () => {
  seed([]);
  api.connect.mockRejectedValueOnce(new Error("OpenRouter rejected this key"));
  api.connect.mockResolvedValueOnce({ ...connection, provider: "openrouter" });
  const view = await mount();
  try {
    expect(view.container.textContent).toContain("Provided by your organization");
    const row = view.container.querySelector('[data-testid="model-key-openrouter"]');
    await act(async () => {
      [...(row?.querySelectorAll("button") ?? [])]
        .find((b) => b.textContent === "Add key")
        ?.click();
    });
    const input = view.container.querySelector('input[type="password"]') as HTMLInputElement;
    expect(input.autocomplete).toBe("off");
    await act(async () => setValue(input, "sk-or-bad"));
    await act(async () => view.button("Save")?.click());
    expect(api.connect).toHaveBeenCalledWith({ provider: "openrouter", apiKey: "sk-or-bad" });
    expect(view.container.querySelector('[role="alert"]')?.textContent).toBe(
      "OpenRouter rejected this key",
    );
    await act(async () => setValue(input, "sk-or-good"));
    await act(async () => view.button("Save")?.click());
    expect(api.connect).toHaveBeenLastCalledWith({ provider: "openrouter", apiKey: "sk-or-good" });
    expect(view.container.querySelector('input[type="password"]')).toBeNull();
    expect(view.onChanged).toHaveBeenCalled();
  } finally {
    await view.cleanup();
  }
});

it("removes a key", async () => {
  seed();
  api.disconnect.mockResolvedValue({ ok: true });
  const view = await mount();
  try {
    await act(async () => view.button("Remove")?.click());
    expect(api.disconnect).toHaveBeenCalledWith({ provider: "anthropic" });
  } finally {
    await view.cleanup();
  }
});

it("switches the harness, then shows the new harness's models once the status follows", async () => {
  seed();
  api.setHarness.mockResolvedValue({ ok: true });
  const view = await mount();
  try {
    const runsOn = view.container.querySelector('[data-testid="runs-on"]');
    expect(runsOn?.getAttribute("aria-label")).toBe("Runs on");
    const claude = view.container.querySelector(
      '[data-testid="runs-on-claude-sdk"]',
    ) as HTMLButtonElement;
    const pi = view.container.querySelector('[data-testid="runs-on-pi"]') as HTMLButtonElement;
    expect([claude.textContent, pi.textContent]).toEqual(["Claude", "Pi"]);
    expect(pi.getAttribute("aria-pressed")).toBe("true");

    api.catalog.mockResolvedValue({ ...catalog, harness: "claude-sdk" });
    await act(async () => claude.click());
    expect(api.setHarness).toHaveBeenCalledWith({ harness: "claude-sdk" });
    expect(view.onChanged).toHaveBeenCalled();
    expect(claude.getAttribute("aria-pressed")).toBe("true");

    await view.rerender({ ...status, harnesses: [{ id: "claude-sdk", label: "Claude" }] });
    expect(api.catalog).toHaveBeenLastCalledWith({ harness: "claude-sdk" });
    expect(
      view.container.querySelector('[data-testid="foundation-model-claude-sdk"]'),
    ).not.toBeNull();
    expect(view.container.querySelector('[data-testid="foundation-model-pi"]')).toBeNull();
  } finally {
    await view.cleanup();
  }
});

it("keeps the harness and shows the engine's message while a reply runs", async () => {
  seed();
  api.setHarness.mockRejectedValueOnce(new Error("Finish or stop the current reply first."));
  const view = await mount();
  try {
    await act(async () =>
      (view.container.querySelector('[data-testid="runs-on-claude-sdk"]') as HTMLElement).click(),
    );
    expect(view.onChanged).not.toHaveBeenCalled();
    expect(
      view.container.querySelector('[data-testid="runs-on-pi"]')?.getAttribute("aria-pressed"),
    ).toBe("true");
    expect(view.container.querySelector('[role="alert"]')?.textContent).toBe(
      "Finish or stop the current reply first.",
    );
  } finally {
    await view.cleanup();
  }
});

it("shows Foundation, Builder and Light, each automatic until picked", async () => {
  seed();
  api.setPick.mockResolvedValue({ ok: true });
  const view = await mount();
  try {
    const select = (category: string) =>
      view.container.querySelector(
        `select[data-testid="${category}-model-pi"]`,
      ) as HTMLSelectElement;
    expect(
      ["foundation", "builder", "light"].map((c) => select(c).getAttribute("aria-label")),
    ).toEqual(["Foundation", "Builder", "Light"]);
    expect(select("foundation").options[0]?.textContent).toBe("Automatic · Sonnet 5");
    expect(select("light").options[0]?.textContent).toBe("Automatic · Haiku 4");
    expect(select("builder").value).toBe("");
    await act(async () => setValue(select("foundation"), "haiku-4"));
    expect(api.setPick).toHaveBeenCalledWith({
      harness: "pi",
      category: "foundation",
      model: "haiku-4",
    });
    await act(async () => setValue(select("light"), ""));
    expect(api.setPick).toHaveBeenLastCalledWith({ harness: "pi", category: "light", model: null });
  } finally {
    await view.cleanup();
  }
});

it("shows the person's own pick and puts it back when saving fails", async () => {
  seed();
  api.catalog.mockResolvedValue({
    ...catalog,
    picks: { ...catalog.picks, builder: "haiku-4" },
    builderModel: "haiku-4",
  });
  api.setPick.mockRejectedValueOnce(new Error("This model isn't available — pick another."));
  const view = await mount();
  try {
    const select = view.container.querySelector(
      'select[data-testid="builder-model-pi"]',
    ) as HTMLSelectElement;
    expect(select.value).toBe("haiku-4");
    await act(async () => setValue(select, "sonnet-5"));
    expect(select.value).toBe("haiku-4");
    expect(view.container.querySelector('[role="alert"]')?.textContent).toBe(
      "This model isn't available — pick another.",
    );
  } finally {
    await view.cleanup();
  }
});

it("shows the budget, saves a new limit and the action at the limit, and the organization's cap", async () => {
  seed();
  api.setBudget.mockImplementation(async (input) => ({
    ...input,
    spentMonthUsd: 5,
    orgLimitUsd: 100,
  }));
  const view = await mount();
  try {
    expect(view.container.querySelector('[data-testid="budget-spend"]')?.textContent).toContain(
      "$5 of $20",
    );
    expect(
      view.container.querySelector('[role="progressbar"]')?.getAttribute("aria-valuenow"),
    ).toBe("5");
    expect(view.container.textContent).toContain("Organization limit");
    const limit = view.container.querySelector('input[inputmode="decimal"]') as HTMLInputElement;
    await act(async () => setValue(limit, "50"));
    await act(async () => {
      limit.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
    });
    expect(api.setBudget).toHaveBeenCalledWith({ monthlyLimitUsd: 50, atLimit: "ask" });
    await act(async () =>
      (view.container.querySelector('[data-testid="budget-at-limit-stop"]') as HTMLElement).click(),
    );
    expect(api.setBudget).toHaveBeenLastCalledWith({ monthlyLimitUsd: 50, atLimit: "stop" });
    await act(async () => setValue(limit, ""));
    await act(async () => {
      limit.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
    });
    expect(api.setBudget).toHaveBeenLastCalledWith({ monthlyLimitUsd: null, atLimit: "stop" });
  } finally {
    await view.cleanup();
  }
});
