// @vitest-environment jsdom

import type { ComponentProps, ReactNode } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../test/i18n")).withI18nRoot(await orig<typeof import("react-dom/client")>()),
);

const api = vi.hoisted(() => ({ rules: vi.fn(), revoke: vi.fn(), setSpending: vi.fn() }));
vi.mock("../../lib/rpc", () => ({ rpc: { approvals: api } }));
vi.mock("@lingui/react/macro", () => {
  const t = (parts: TemplateStringsArray, ...values: unknown[]) =>
    parts.reduce((acc, part, i) => `${acc}${part}${values[i] ?? ""}`, "");
  return {
    useLingui: () => ({ t, i18n: { locale: "en" } }),
    Trans: ({ children }: { children: ReactNode }) => children,
  };
});
vi.mock("@superchat/ui-web", () => ({
  Button: (props: ComponentProps<"button">) => <button {...props} />,
  cn: (...classes: unknown[]) => classes.filter(Boolean).join(" "),
}));

import { ApprovalsSettings } from "./ApprovalsSettings";

it("lists standing rules with Revoke and saves the daily spending limit", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  api.rules.mockResolvedValue({
    rules: [{ id: "r1", label: "Send messages to bob@acme.test", decision: "allow", createdAt: 1 }],
    spending: { dailyCapUsd: 0, spentTodayUsd: 0 },
  });
  api.revoke.mockResolvedValue({ ok: true });
  api.setSpending.mockResolvedValue({ dailyCapUsd: 25, spentTodayUsd: 0 });
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => {
      root.render(<ApprovalsSettings botId="bot-1" />);
    });
    await act(async () => {
      await vi.waitFor(() =>
        expect(container.textContent).toContain("Send messages to bob@acme.test"),
      );
    });
    expect(api.rules).toHaveBeenCalledWith({ botId: "bot-1" });

    const input = container.querySelector("input") as HTMLInputElement;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      setter?.call(input, "25");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => {
      input.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
    });
    expect(api.setSpending).toHaveBeenCalledWith({ botId: "bot-1", dailyCapUsd: 25 });

    const revoke = [...container.querySelectorAll("button")].find(
      (button) => button.textContent === "Revoke",
    );
    await act(async () => revoke?.click());
    expect(api.revoke).toHaveBeenCalledWith({ botId: "bot-1", ruleId: "r1" });
    expect(container.textContent).not.toContain("Send messages to bob@acme.test");
  } finally {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  }
});
