// @vitest-environment jsdom

import type { ArtifactVersion } from "@superchat/contracts";
import type { ReactNode } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ShowMessageProvider } from "../../../lib/show-message";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../../test/i18n")).withI18nRoot(
    await orig<typeof import("react-dom/client")>(),
  ),
);

import { VersionMenu } from "./VersionMenu";

const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString();
const VERSIONS: ArtifactVersion[] = [
  { id: "v3", version: 3, name: "deck.html", createdAt: ago(3), origin: "ai", itemId: "msg_7" },
  { id: "v2", version: 2, name: "deck.html", createdAt: ago(9), origin: "manual", itemId: null },
  { id: "v1", version: 1, name: "deck.html", createdAt: ago(20), origin: "ai" },
];

const roots: Root[] = [];
async function mount(node: ReactNode) {
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  roots.push(root);
  await act(async () => root.render(node));
}
const all = (testId: string) => [
  ...document.querySelectorAll<HTMLElement>(`[data-testid="${testId}"]`),
];
async function openMenu() {
  const trigger = document.querySelector<HTMLElement>('[data-testid="version-menu"]');
  await act(async () => trigger?.click());
  await act(async () => {});
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  document.body.innerHTML = "";
});
afterEach(async () => {
  for (const root of roots.splice(0)) await act(async () => root.unmount());
});

function menu(props: { showMessage?: (id: string) => void; versions?: ArtifactVersion[] }) {
  const onPick = vi.fn();
  const onRestore = vi.fn(async () => undefined);
  const node = (
    <VersionMenu
      kindLabel="Deck"
      version={3}
      versions={props.versions ?? VERSIONS}
      shownId="v3"
      onPick={onPick}
      onRestore={onRestore}
    />
  );
  return {
    onPick,
    onRestore,
    node: props.showMessage ? (
      <ShowMessageProvider value={props.showMessage}>{node}</ShowMessageProvider>
    ) : (
      node
    ),
  };
}

it("says a version came from your message, and choosing that line shows the message", async () => {
  const showMessage = vi.fn();
  const { node, onPick, onRestore } = menu({ showMessage });
  await mount(node);
  await openMenu();
  const links = all("version-message");
  expect(links).toHaveLength(1);
  expect(links[0]?.textContent).toBe("from your message · 3m ago");
  // The others keep their origin under the version.
  expect(all("version-item")[1]?.textContent).toContain("by you");
  await act(async () => links[0]?.click());
  expect(showMessage).toHaveBeenCalledWith("msg_7");
  expect(onPick).not.toHaveBeenCalled();
  expect(onRestore).not.toHaveBeenCalled();
});

it("keeps picking a version on its own row", async () => {
  const { node, onPick } = menu({ showMessage: vi.fn() });
  await mount(node);
  await openMenu();
  await act(async () => all("version-item")[0]?.click());
  expect(onPick).toHaveBeenCalledWith("v3");
});

it("without a conversation to scroll, the line is plain text under the version", async () => {
  const { node } = menu({});
  await mount(node);
  await openMenu();
  expect(all("version-message")).toHaveLength(0);
  expect(all("version-item")[0]?.textContent).toContain("from your message · 3m ago");
});

it("a single version from a message still opens the menu to reach it", async () => {
  const first = VERSIONS[0] as ArtifactVersion;
  const { node } = menu({ showMessage: vi.fn(), versions: [first] });
  await mount(node);
  expect(document.querySelector('[data-testid="version-menu"]')).not.toBeNull();
  const { node: plain } = menu({ versions: [first] });
  document.body.innerHTML = "";
  await mount(plain);
  expect(document.querySelector('[data-testid="version-label"]')?.textContent).toBe(
    "Deck · version 3",
  );
});
