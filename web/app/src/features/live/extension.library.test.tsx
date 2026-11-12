// @vitest-environment jsdom

import { i18n } from "@lingui/core";
import { I18nProvider } from "@lingui/react";
import type { Artifact, LivePage } from "@superchat/contracts";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ get: vi.fn(), pages: vi.fn() }));
vi.mock("../../lib/rpc", () => ({ rpc: { live: api } }));

import { liveExtension } from "./extension";
import { resetLivePages } from "./useLivePages";

i18n.loadAndActivate({ locale: "en", messages: {} });

const livePage = (over: Partial<LivePage>): LivePage => ({
  artifactId: "a1",
  name: "p.html",
  title: "P",
  taskId: "t1",
  schedule: null,
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
const artifact = (id: string) => ({ id, name: "p.html", mimeType: "text/html" }) as Artifact;

function Probe({ item }: { item: Artifact }) {
  const card = liveExtension.useCard?.(item);
  const facet = liveExtension.useLibraryFacet?.();
  return (
    <div>
      {card?.badge}
      <i data-testid="facet">{facet ? `${facet.label}:${facet.matches(item)}` : "none"}</i>
    </div>
  );
}

function Note({ version }: { version: number }) {
  const parts = liveExtension.usePanel({
    artifact: artifact("a1"),
    newestVersion: {
      id: "v",
      version,
      name: "p.html",
      createdAt: new Date(500_000).toISOString(),
    },
  });
  return <div data-testid="note">{parts.versionNote}</div>;
}

let host: HTMLDivElement;
beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div");
  document.body.append(host);
});
afterEach(() => {
  resetLivePages();
  api.get.mockReset();
  api.pages.mockReset();
  document.body.innerHTML = "";
});

async function render(node: React.ReactNode) {
  await act(async () => {
    createRoot(host).render(<I18nProvider i18n={i18n}>{node}</I18nProvider>);
  });
}
const text = (id: string) => document.querySelector(`[data-testid="${id}"]`)?.textContent;

it("tags a live page and keeps a Live filter for it", async () => {
  api.pages.mockResolvedValue([livePage({})]);
  await render(<Probe item={artifact("a1")} />);
  expect(text("live-badge")).toBe("Live");
  expect(text("facet")).toBe("Live:true");
});

it("says when the update failed or the page is paused, and leaves other files alone", async () => {
  api.pages.mockResolvedValue([
    livePage({ failures: 1, lastFailure: { at: 1, reason: "x" } }),
    livePage({ artifactId: "a2", paused: true }),
  ]);
  await render(
    <>
      <Probe item={artifact("a1")} />
      <Probe item={artifact("a2")} />
      <Probe item={artifact("a3")} />
    </>,
  );
  const badges = [...document.querySelectorAll('[data-testid="live-badge"]')].map(
    (el) => el.textContent,
  );
  expect(badges).toEqual(["Live · update failed", "Live · paused"]);
  expect(
    [...document.querySelectorAll('[data-testid="facet"]')].map((el) => el.textContent),
  ).toEqual(["Live:true", "Live:true", "Live:false"]);
});

const liveState = (checkedAt: number, updateVersion: number | null) => ({
  schedule: null,
  active: true,
  sources: [],
  tell: "notable",
  followPublish: false,
  note: "",
  onChange: false,
  checkedAt,
  openIssue: null,
  lastUpdate: updateVersion
    ? { version: updateVersion, applied: [], suggestions: [], at: 1 }
    : null,
});

it("notes 'nothing new' when the last check is newer than the newest version", async () => {
  api.get.mockResolvedValue(liveState(1000, 2));
  await render(<Note version={3} />);
  expect(text("note")).toContain("nothing new");
});

it("has no note when the newest version is the update itself, or newer than the check", async () => {
  api.get.mockResolvedValue(liveState(1000, 3));
  await render(<Note version={3} />);
  expect(text("note")).toBe("");
  api.get.mockResolvedValue(liveState(100, 2));
  document.body.innerHTML = "";
  host = document.createElement("div");
  document.body.append(host);
  await render(<Note version={3} />);
  expect(text("note")).toBe("");
});
