// @vitest-environment jsdom

import type { Bot } from "@superchat/contracts";
import type { ComponentProps, ReactNode } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../test/i18n")).withI18nRoot(await orig<typeof import("react-dom/client")>()),
);

vi.mock("@lingui/react/macro", () => {
  const t = (parts: TemplateStringsArray) => parts.join("");
  return { useLingui: () => ({ t }), Trans: ({ children }: { children: ReactNode }) => children };
});

vi.mock("@superchat/ui-web", () => ({
  cn: (...classes: unknown[]) => classes.filter(Boolean).join(" "),
  Button: (props: ComponentProps<"button">) => <button type="button" {...props} />,
  Dialog: ({ children }: { children?: ReactNode }) => <>{children}</>,
  DialogContent: ({
    children,
    "data-testid": testId,
    "data-settings-section": section,
  }: {
    children?: ReactNode;
    "data-testid"?: string;
    "data-settings-section"?: string;
  }) => (
    <div data-testid={testId} data-settings-section={section}>
      {children}
    </div>
  ),
  DialogClose: ({
    children,
    "aria-label": ariaLabel,
  }: {
    children?: ReactNode;
    "aria-label"?: string;
  }) => (
    <button type="button" aria-label={ariaLabel}>
      {children}
    </button>
  ),
  DialogTitle: ({ children }: { children?: ReactNode }) => <h2>{children}</h2>,
}));

// Every panel below is presentational chrome not under test here: this file only
// exercises the nav (which sections exist, in which order) and the wiring into the
// Superchat section.
vi.mock("./app/settings/GeneralPanel", () => ({
  GeneralPanel: () => <div data-testid="general-panel" />,
}));
vi.mock("./app/settings/HostStatusPanel", () => ({
  HostStatusPanel: () => <div data-testid="machine-panel" />,
}));
vi.mock("./app/SuperchatSettingsPanel", () => ({
  SuperchatSettingsPanel: ({ bot }: { bot: Bot }) => (
    <div data-testid="superchat-panel" data-bot-name={bot.name} />
  ),
}));
const engine = vi.hoisted(() => ({ status: null as unknown }));
vi.mock("../features/models", () => ({
  useEngineModelsStatus: () => [engine.status, () => undefined, true],
  ModelsPanel: () => <div data-testid="engine-models-panel" />,
}));

import { SettingsOverlay } from "./SettingsOverlay";

const museBot: Bot = {
  id: "session-1",
  name: "Superchat",
  title: "",
  description: "",
  color: "#0090FF",
  unread: false,
  preview: "",
  status: "idle",
  updatedAt: new Date().toISOString(),
  createdAt: new Date().toISOString(),
};

function render() {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  return { container, root };
}

function navIds(container: HTMLElement) {
  return [...container.querySelectorAll("[data-testid^='settings-nav-']")].map((el) =>
    (el.getAttribute("data-testid") ?? "").replace("settings-nav-", ""),
  );
}

const baseProps = {
  name: "Person",
  museBot,
  onClose: () => undefined,
};

it("lists Superchat first, then General and Machine", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  engine.status = null;
  const { container, root } = render();
  try {
    await act(async () => root.render(<SettingsOverlay {...baseProps} />));
    expect(navIds(container)).toEqual(["superchat", "general", "machine"]);
    expect(
      container.querySelector('[data-testid="settings-nav-superchat"]')?.textContent,
    ).toContain("Superchat");
  } finally {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  }
});

it("opening Superchat renders the panel wired to the Muse", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  engine.status = null;
  const { container, root } = render();
  try {
    await act(async () => root.render(<SettingsOverlay {...baseProps} />));
    await act(async () =>
      container.querySelector<HTMLButtonElement>('[data-testid="settings-nav-superchat"]')?.click(),
    );
    expect(
      container.querySelector('[data-testid="superchat-panel"]')?.getAttribute("data-bot-name"),
    ).toBe("Superchat");
  } finally {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  }
});

it("offers Models when the engine has them", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const { container, root } = render();
  try {
    engine.status = { enabled: true, harnesses: [], ready: true };
    await act(async () => root.render(<SettingsOverlay {...baseProps} />));
    expect(navIds(container)).toEqual(["superchat", "general", "models", "machine"]);
    await act(async () =>
      container.querySelector<HTMLButtonElement>('[data-testid="settings-nav-models"]')?.click(),
    );
    expect(container.querySelector('[data-testid="engine-models-panel"]')).toBeTruthy();
  } finally {
    engine.status = null;
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  }
});

it("opens at Models when asked to, and falls back to General without engine models", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const { container, root } = render();
  try {
    engine.status = { enabled: true, harnesses: [], ready: false };
    await act(async () => root.render(<SettingsOverlay {...baseProps} initialSection="models" />));
    expect(container.querySelector('[data-testid="engine-models-panel"]')).toBeTruthy();
    engine.status = { enabled: false, harnesses: [], ready: false };
    await act(async () => root.render(<SettingsOverlay {...baseProps} initialSection="models" />));
    expect(container.querySelector('[data-testid="engine-models-panel"]')).toBeNull();
    expect(container.querySelector('[data-settings-section="general"]')).toBeTruthy();
  } finally {
    engine.status = null;
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  }
});
