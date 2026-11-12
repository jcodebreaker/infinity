// @vitest-environment jsdom

import type { Bot } from "@superchat/contracts";
import type { ReactNode } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../test/i18n")).withI18nRoot(await orig<typeof import("react-dom/client")>()),
);

vi.mock("../../lib/use-user-id", () => ({ useUserId: () => "user-1" }));
vi.mock("../../features/approvals", () => ({ ApprovalsSettings: () => null }));
vi.mock("../../features/vault", () => ({ VaultSettings: () => null }));

vi.mock("@lingui/react/macro", () => {
  const t = (parts: TemplateStringsArray) => parts.join("");
  return { useLingui: () => ({ t }), Trans: ({ children }: { children: ReactNode }) => children };
});

vi.mock("@superchat/ui-web", () => ({
  cn: (...parts: unknown[]) => parts.filter(Boolean).join(" "),
}));

vi.mock("../../components/ai/orb", () => ({
  SuperchatOrb: () => <span data-testid="superchat-orb" />,
}));

import { SuperchatSettingsPanel } from "./SuperchatSettingsPanel";

const bot: Bot = {
  id: "session-1",
  name: "superchat",
  title: "",
  description: "",
  color: "#0090FF",
  unread: false,
  preview: "",
  status: "idle",
  updatedAt: new Date().toISOString(),
  createdAt: new Date().toISOString(),
};

it("shows the orb and the Muse's name, not a color picker or a name field", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<SuperchatSettingsPanel bot={bot} userId="user-1" />));
    expect(container.querySelector('[data-testid="superchat-orb"]')).not.toBeNull();
    expect(container.querySelector("h3")?.textContent).toBe("superchat");
    expect(container.querySelector("input")).toBeNull();
    expect(container.textContent).not.toContain("change the color");
  } finally {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  }
});
