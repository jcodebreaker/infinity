// @vitest-environment jsdom

import type { ReactNode } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../test/i18n")).withI18nRoot(await orig<typeof import("react-dom/client")>()),
);

import { type FolderEntry, useChatPlace, WorkInChip, type WorkPlace } from "./WorkIn";

const dir = (path: string): FolderEntry => ({
  name: path.split("/").pop() ?? path,
  path,
  type: "directory",
});
const file = (path: string): FolderEntry => ({
  name: path.split("/").pop() ?? path,
  path,
  type: "file",
});

/** A workspace with one Project (`projects/q3` has a card), a stray folder under `projects/`,
 * and `reports/2026` to drill into. */
const TREE: Record<string, FolderEntry[]> = {
  "": [dir("projects"), dir("reports"), dir(".cache"), file("notes.md")],
  projects: [dir("projects/q3"), dir("projects/stray")],
  "projects/q3": [file("projects/q3/PROJECT.md")],
  "projects/stray": [],
  reports: [dir("reports/2026")],
  "reports/2026": [],
};

const roots: Root[] = [];
async function mount(node: ReactNode) {
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  roots.push(root);
  await act(async () => root.render(node));
  await act(async () => {});
  return host;
}

const settle = () => act(async () => {});
const option = (text: string) =>
  [...document.querySelectorAll<HTMLElement>("[cmdk-item]")].find(
    (item) => item.textContent?.trim() === text,
  );
async function choose(text: string) {
  const item = option(text);
  if (!item) throw new Error(`no option "${text}" in ${document.body.textContent}`);
  await act(async () => item.click());
  await settle();
}
async function openPicker(testId: string) {
  const trigger = document.querySelector<HTMLButtonElement>(`[data-testid=${testId}]`);
  await act(async () => trigger?.click());
  await settle();
  await settle();
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
  Element.prototype.scrollIntoView ??= () => undefined;
  document.body.innerHTML = "";
});

afterEach(async () => {
  for (const root of roots.splice(0)) await act(async () => root.unmount());
});

const list = vi.fn(async (path: string) => TREE[path] ?? []);

/** The Conversation's chip as the Shell wires it: the place is read back after every move. */
function Chip({
  load,
  onPick,
}: {
  load: () => Promise<WorkPlace>;
  onPick: (folder: string | null) => Promise<unknown>;
}) {
  const { place, reload } = useChatPlace(load, "bot-1", 0);
  return <WorkInChip place={place} list={list} onPick={(folder) => onPick(folder).then(reload)} />;
}

it("lists Projects and drills into folders, then moves the Conversation and says where it works", async () => {
  const place: WorkPlace = { project: null, folder: null, chosen: false };
  const setFolder = vi.fn(async (folder: string | null) => {
    place.folder = folder;
    place.chosen = folder !== null;
  });
  await mount(<Chip load={async () => ({ ...place })} onPick={setFolder} />);
  const chip = () => document.querySelector("[data-testid=work-in-chip]");
  expect(chip()?.textContent).toBe("Work in…");

  await openPicker("work-in-chip");
  // Projects carry a card; hidden folders and files are left out.
  const items = [...document.querySelectorAll("[cmdk-item]")].map((i) => i.textContent?.trim());
  expect(items).toEqual(["Whole workspace", "q3", "reports"]);

  await choose("reports");
  await choose("2026");
  await choose("Work in 2026");
  await settle();
  expect(setFolder).toHaveBeenCalledWith("reports/2026");
  expect(chip()?.textContent).toBe("Working in 2026");

  await openPicker("work-in-chip");
  await choose("Whole workspace");
  await settle();
  expect(setFolder).toHaveBeenLastCalledWith(null);
  expect(chip()?.textContent).toBe("Work in…");
});
