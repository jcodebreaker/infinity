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

import { Composer } from "./Composer";

vi.stubGlobal(
  "ResizeObserver",
  class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
);

async function render(
  failure: { id: string; code: string } | null,
  onSend: (text: string) => Promise<boolean | undefined> = async () => undefined,
) {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  await act(async () => {
    root.render(<Composer phase="idle" failure={failure} onSend={onSend} />);
  });
  return { host, cleanup: () => act(async () => root.unmount()) };
}

it("offers Add your API key and Raise budget on the failed reply, opening Settings > Models", async () => {
  const opened: unknown[] = [];
  const listener = (event: Event) => opened.push((event as CustomEvent<unknown>).detail);
  window.addEventListener("superchat:open-settings", listener);
  try {
    const key = await render({ id: "m1", code: "model_key_required" });
    const keyButton = key.host.querySelector(
      '[data-testid="composer-error-action"]',
    ) as HTMLElement;
    expect(keyButton.textContent).toBe("Add your API key");
    await act(async () => keyButton.click());
    await key.cleanup();

    const budget = await render({ id: "m2", code: "model_budget_exhausted" });
    const budgetButton = budget.host.querySelector(
      '[data-testid="composer-error-action"]',
    ) as HTMLElement;
    expect(budgetButton.textContent).toBe("Raise budget");
    await act(async () => budgetButton.click());
    await budget.cleanup();
    expect(opened).toEqual(["models", "models"]);
  } finally {
    window.removeEventListener("superchat:open-settings", listener);
  }
});

it("shows the paused-account and unsupported-model notes without a fix button", async () => {
  for (const [code, copy] of [
    ["account_suspended", "Your account is paused. Contact your admin."],
    ["model_not_supported", "This model isn't available — pick another."],
  ] as const) {
    const view = await render({ id: code, code });
    expect(view.host.querySelector('[data-testid="composer-error"]')?.textContent).toContain(copy);
    expect(view.host.querySelector('[data-testid="composer-error-action"]')).toBeNull();
    await view.cleanup();
  }
});

it("gives the person's words back when the send fails, and clears them when it works", async () => {
  const view = await render(null, async () => false);
  const box = view.host.querySelector("textarea") as HTMLTextAreaElement;
  await act(async () => {
    const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    set?.call(box, "what is the Northwind fee?");
    box.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => {
    box.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  });
  expect(box.value).toBe("what is the Northwind fee?");
  await view.cleanup();

  const ok = await render(null, async () => true);
  const okBox = ok.host.querySelector("textarea") as HTMLTextAreaElement;
  await act(async () => {
    const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    set?.call(okBox, "hello");
    okBox.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => {
    okBox.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  });
  expect(okBox.value).toBe("");
  await ok.cleanup();
});
