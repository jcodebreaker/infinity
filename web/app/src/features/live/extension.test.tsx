// @vitest-environment jsdom

import { i18n } from "@lingui/core";
import { I18nProvider } from "@lingui/react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  get: vi.fn(),
  save: vi.fn(),
  undo: vi.fn(),
  accept: vi.fn(),
  publish: vi.fn(),
}));
vi.mock("../../lib/rpc", () => ({ rpc: { live: api } }));

import type { Artifact } from "@superchat/contracts";
import * as state from "../artifacts/editor/editor-state";
import { artifactKey } from "../artifacts/editor/editor-state";
import { liveExtension } from "./extension";

i18n.loadAndActivate({ locale: "en", messages: {} });

const page = {
  id: "a1",
  botId: "b1",
  name: "report.html",
  mimeType: "text/html",
  version: 3,
} as Artifact;

const live = {
  schedule: { frequency: "weekly", time: "08:00", weekday: 0 },
  active: true,
  sources: [],
  tell: "notable",
  followPublish: false,
  onChange: true,
  note: "",
  held: null,
  checkedAt: 100,
  openIssue: "Could not place these changes on the page: x",
  lastUpdate: {
    version: 3,
    applied: [{ id: "total", before: "1", after: "2", source: null, notable: true }],
    suggestions: [{ id: "name", yours: "A", sourceValue: "B", source: null }],
    at: 90,
  },
};

function Panel({ artifact }: { artifact: Artifact }) {
  const parts = liveExtension.usePanel({ artifact });
  return (
    <div>
      {parts.badge}
      {parts.actions}
      {parts.below}
    </div>
  );
}

let host: HTMLDivElement;
beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div");
  document.body.append(host);
});
afterEach(() => {
  for (const mock of Object.values(api)) mock.mockReset();
  document.body.innerHTML = "";
});

async function show(artifact: Artifact) {
  await act(async () => {
    createRoot(host).render(
      <I18nProvider i18n={i18n}>
        <Panel artifact={artifact} />
      </I18nProvider>,
    );
  });
}
const byTest = (id: string) => document.querySelector<HTMLElement>(`[data-testid="${id}"]`);

it("shows the tag, the update bar, a chip that focuses its part, and the open issue", async () => {
  api.get.mockResolvedValue(live);
  const focus = vi.spyOn(state, "focusArtifactPart");
  await show(page);
  expect(byTest("live-tag")?.textContent).toBe("Live");
  expect(byTest("live-tag")?.getAttribute("title")).toMatch(/^Updated /);
  expect(byTest("live-settings")?.textContent).toBe("Updates when file changes");
  expect(byTest("live-bar")?.textContent).toContain("1 change");
  expect(byTest("live-suggestion")?.textContent).toContain("Source says B, you wrote A");
  expect(byTest("live-issue")?.textContent).toContain("Could not place");
  expect(byTest("live-chip")?.textContent).toBe("1 → 2");
  await act(async () => byTest("live-chip")?.click());
  expect(focus).toHaveBeenCalledWith(artifactKey(page), "total");
});

it("only offers 'Keep updated' on a page that is not live, and nothing on a deck", async () => {
  api.get.mockResolvedValue(null);
  await show(page);
  expect(byTest("live-settings")?.textContent).toBe("Keep updated");
  expect(byTest("live-tag")).toBeNull();
  expect(byTest("live-bar")).toBeNull();
  api.get.mockClear();
  document.body.innerHTML = "";
  host = document.createElement("div");
  document.body.append(host);
  await show({ ...page, name: "talk.deck.html" });
  expect(api.get).not.toHaveBeenCalled();
  expect(byTest("live-settings")).toBeNull();
});

it("hides the changes bar when the shown version is older than the update", async () => {
  api.get.mockResolvedValue(live);
  await show({ ...page, version: 2 });
  expect(byTest("live-tag")).not.toBeNull();
  expect(byTest("live-chip")).toBeNull();
  expect(byTest("live-suggestion")).not.toBeNull();
});

it("undo asks the engine", async () => {
  api.get.mockResolvedValue(live);
  api.undo.mockResolvedValue({ id: "a4", version: 4 });
  await show(page);
  const undo = [...document.querySelectorAll("button")].find((b) =>
    b.textContent?.includes("Undo this update"),
  );
  await act(async () => undo?.click());
  expect(api.undo).toHaveBeenCalledWith({ artifactId: "a1" });
});

it("a held update says why the published copy did not take it, and Publish publishes it", async () => {
  api.get.mockResolvedValue({ ...live, held: { version: 3, reason: "edits" } });
  api.publish.mockResolvedValue({ ...live, held: null });
  await show(page);
  expect(byTest("live-held")?.textContent).toContain("Published copy not updated.");
  expect(byTest("live-held")?.textContent).toContain("edits of yours that are not published");
  await act(async () => byTest("live-held")?.querySelector("button")?.click());
  expect(api.publish).toHaveBeenCalledWith({ artifactId: "a1" });
  expect(byTest("live-held")).toBeNull();
});

it("a sheet can be kept updated like a page", async () => {
  api.get.mockResolvedValue(null);
  await show({ ...page, name: "teas.csv", mimeType: "text/csv" });
  expect(api.get).toHaveBeenCalledWith({ artifactId: "a1" });
  expect(byTest("live-settings")?.textContent).toBe("Keep updated");
});
