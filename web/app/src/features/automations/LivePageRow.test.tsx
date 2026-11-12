// @vitest-environment jsdom

import { i18n } from "@lingui/core";
import { I18nProvider } from "@lingui/react";
import type { LivePage } from "@superchat/contracts";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ runs: vi.fn(), listVersions: vi.fn() }));
vi.mock("../../lib/rpc", () => ({
  rpc: { automations: { runs: api.runs }, artifacts: { listVersions: api.listVersions } },
}));

import { LivePageRow } from "./LivePageRow";

i18n.loadAndActivate({ locale: "en", messages: {} });

const page = (over: Partial<LivePage> = {}): LivePage => ({
  artifactId: "a1",
  name: "p.html",
  title: "Prices",
  taskId: "t1",
  schedule: { frequency: "weekly", time: "08:00", weekday: 0 },
  onChange: false,
  sources: [],
  checkedAt: 1000,
  failures: 0,
  lastFailure: null,
  paused: false,
  openIssue: null,
  lastUpdate: null,
  ...over,
});

let host: HTMLDivElement;
beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div");
  document.body.append(host);
});
afterEach(() => {
  api.runs.mockReset();
  api.listVersions.mockReset();
  document.body.innerHTML = "";
});

const handlers = () => ({
  onOpenPage: vi.fn(),
  onOpenRun: vi.fn(),
  onResume: vi.fn(),
});

async function show(p: LivePage, h = handlers()) {
  await act(async () => {
    createRoot(host).render(
      <I18nProvider i18n={i18n}>
        <ul>
          <LivePageRow page={p} botId="b1" {...h} />
        </ul>
      </I18nProvider>,
    );
  });
  return h;
}
const byTest = (id: string) => document.querySelector<HTMLElement>(`[data-testid="${id}"]`);
const button = (text: string) =>
  [...document.querySelectorAll("button")].find((b) => b.textContent?.includes(text));

it("shows the title with a Live page tag, and opens the page from the title", async () => {
  const h = await show(page());
  expect(byTest("live-page-title")?.textContent).toBe("Keeps Prices updated");
  expect(host.textContent).toContain("Live page");
  expect(host.textContent).toContain("Checked · nothing new");
  await act(async () => byTest("live-page-title")?.click());
  expect(h.onOpenPage).toHaveBeenCalledWith("a1");
});

it("a failed page shows the reason in the destructive tone", async () => {
  await show(page({ failures: 1, lastFailure: { at: 5, reason: "source missing" } }));
  expect(byTest("live-status")?.textContent).toContain("Failed · source missing");
  expect(byTest("live-status")?.className).toContain("text-destructive");
});

it("a paused page says so and Resume asks to resume", async () => {
  const h = await show(page({ paused: true, failures: 3 }));
  expect(byTest("live-status")?.textContent).toContain("Paused after 3 failed runs");
  await act(async () => button("Resume")?.click());
  expect(h.onResume).toHaveBeenCalled();
});

it("expands to the runs: a version link and Open run", async () => {
  api.runs.mockResolvedValue([
    {
      id: "r1",
      status: "succeeded",
      scheduledAt: "2026-10-12T12:00:00.000Z",
      firedAt: "2026-10-12T12:00:00.000Z",
      finishedAt: "2026-10-12T12:01:00.000Z",
      errorCode: null,
      chatId: "helper-1",
    },
    {
      id: "r0",
      status: "succeeded",
      scheduledAt: "2026-10-05T12:00:00.000Z",
      firedAt: "2026-10-05T12:00:00.000Z",
      finishedAt: "2026-10-05T12:01:00.000Z",
      errorCode: null,
      chatId: null,
    },
  ]);
  api.listVersions.mockResolvedValue([
    {
      id: "v2",
      version: 2,
      name: "p.html",
      createdAt: "2026-10-12T12:00:30.000Z",
      origin: "update",
    },
  ]);
  const h = await show(page());
  await act(async () =>
    document.querySelector<HTMLElement>('[aria-label="Show the runs"]')?.click(),
  );
  expect(api.runs).toHaveBeenCalledWith({ botId: "b1", id: "t1", limit: 20 });
  const lines = document.querySelectorAll('[data-testid="live-run"]');
  expect(lines).toHaveLength(2);
  expect(lines[0]?.textContent).toContain("Version 2");
  expect(lines[1]?.textContent).toContain("Checked · nothing new");
  await act(async () => button("Open run")?.click());
  expect(h.onOpenRun).toHaveBeenCalledWith("helper-1");
  await act(async () => button("Version 2")?.click());
  expect(h.onOpenPage).toHaveBeenCalledWith("v2");
});
