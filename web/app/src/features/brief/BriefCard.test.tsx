// @vitest-environment jsdom

import type { LatestBrief } from "@superchat/contracts";
import type { ReactNode } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../test/i18n")).withI18nRoot(await orig<typeof import("react-dom/client")>()),
);
vi.mock("../../lib/rpc", () => ({ rpc: {} }));
vi.mock("@lingui/react/macro", () => {
  const t = (parts: TemplateStringsArray, ...values: unknown[]) =>
    parts.reduce((acc, part, i) => `${acc}${part}${values[i] ?? ""}`, "");
  return {
    useLingui: () => ({ t, i18n: { locale: "en" } }),
    Trans: ({ children }: { children: ReactNode }) => children,
  };
});

import { FileStudioProvider } from "../../components/cards/context";
import { BriefCard } from "./BriefCard";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const container = document.createElement("div");
document.body.append(container);
const root = createRoot(container);

afterEach(() => {
  act(() => root.render(null));
});

const latest: LatestBrief = {
  taskId: "task-1",
  enabled: true,
  page: {
    artifactId: "v-3",
    title: "Brief · Fri 23 Oct",
    savedAt: new Date().toISOString(),
    line: "Send Dana the revised term sheet",
    home: [],
  },
};

it("shows the newest brief, opens it in the studio and refreshes it", async () => {
  const open = vi.fn();
  const refresh = vi.fn().mockResolvedValue({ ok: true });
  const wire = { latest: vi.fn().mockResolvedValue(latest), refresh };
  await act(async () => {
    root.render(
      <FileStudioProvider value={{ open }}>
        <BriefCard botId="bot-1" wire={wire} />
      </FileStudioProvider>,
    );
  });
  const card = container.querySelector('[data-testid="brief-card"]');
  expect(card?.textContent).toContain("Brief");
  expect(card?.textContent).toContain("Send Dana the revised term sheet");

  const [openButton, refreshButton] = Array.from(card?.querySelectorAll("button") ?? []);
  act(() => openButton?.click());
  expect(open).toHaveBeenCalledWith("v-3", "Brief · Fri 23 Oct");

  await act(async () => refreshButton?.click());
  expect(refresh).toHaveBeenCalledWith({ botId: "bot-1", taskId: "task-1" });
  expect(card?.textContent).toContain("Making a new one…");
});

it("renders nothing before the first brief", async () => {
  const wire = {
    latest: vi.fn().mockResolvedValue({ ...latest, page: null }),
    refresh: vi.fn(),
  };
  await act(async () => root.render(<BriefCard botId="bot-1" wire={wire} />));
  expect(container.querySelector('[data-testid="brief-card"]')).toBeNull();
});
