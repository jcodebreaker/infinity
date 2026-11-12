// @vitest-environment jsdom

import type { MessageArtifact } from "@superchat/contracts";
import type { ReactNode } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../../test/i18n")).withI18nRoot(
    await orig<typeof import("react-dom/client")>(),
  ),
);

const rpc = vi.hoisted(() => ({
  artifacts: { listSpace: vi.fn() },
  chats: { setMessageArtifact: vi.fn() },
}));
vi.mock("../../../lib/rpc", () => ({ rpc }));

import { MessageArtifactChip } from "./MessageArtifactChip";

const LINK: MessageArtifact = {
  artifactId: "art_deck",
  name: "Q3 Lending Review.html",
  version: 4,
  how: "edited",
};
const recent = (id: string, name: string, minutesAgo: number) => ({
  id,
  botId: "bot-1",
  runId: null,
  name,
  description: null,
  mimeType: "text/html",
  size: 1,
  version: 1,
  createdAt: new Date(Date.now() - minutesAgo * 60_000).toISOString(),
  versionCount: 1,
});

const roots: Root[] = [];
async function mount(node: ReactNode) {
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  roots.push(root);
  await act(async () => root.render(node));
  return host;
}
const settle = () => act(async () => {});
const chip = () => document.querySelector<HTMLElement>('[data-testid="message-artifact-chip"]');
const options = () => [
  ...document.querySelectorAll<HTMLButtonElement>('[data-testid="message-artifact-option"]'),
];
async function openChip() {
  await act(async () => chip()?.click());
  await settle();
}
async function choose(text: string) {
  const option = options().find((o) => o.textContent?.includes(text));
  if (!option) throw new Error(`no option "${text}" in ${document.body.textContent}`);
  await act(async () => option.click());
  await settle();
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  document.body.innerHTML = "";
  rpc.artifacts.listSpace.mockReset();
  rpc.chats.setMessageArtifact.mockReset();
  rpc.artifacts.listSpace.mockResolvedValue({
    items: [
      recent("art_memo", "Provisions memo.md", 2),
      recent("art_deck_v4", "Q3 Lending Review.html", 30),
    ],
    nextCursor: null,
  });
});
afterEach(async () => {
  for (const root of roots.splice(0)) await act(async () => root.unmount());
});

it("is a button with the file name and the pointer glyph", async () => {
  await mount(<MessageArtifactChip botId="bot-1" itemId="item_1" artifact={LINK} />);
  expect(chip()?.textContent).toBe("Q3 Lending Review.html");
  expect(chip()?.getAttribute("role")).toBe("button");
  expect(chip()?.querySelector("svg")).not.toBeNull();
  expect(rpc.artifacts.listSpace).not.toHaveBeenCalled();
});

it("lists the recent artifacts newest first plus None, marking the current one", async () => {
  await mount(<MessageArtifactChip botId="bot-1" itemId="item_1" artifact={LINK} />);
  await openChip();
  expect(rpc.artifacts.listSpace).toHaveBeenCalledWith({ botId: "bot-1", limit: 8 });
  const rows = options();
  expect(rows.map((row) => row.textContent?.replace(/\d+m ago$/, ""))).toEqual([
    "Provisions memo.md",
    "Q3 Lending Review.html",
    "None",
  ]);
  expect(rows.map((row) => row.getAttribute("aria-pressed"))).toEqual(["false", "true", "false"]);
  expect(rows[0]?.textContent).toContain("2m ago");
});

it("relinks at once and keeps the engine's answer", async () => {
  let answer: (value: unknown) => void = () => undefined;
  rpc.chats.setMessageArtifact.mockReturnValue(
    new Promise((resolve) => {
      answer = resolve;
    }),
  );
  await mount(<MessageArtifactChip botId="bot-1" itemId="item_1" artifact={LINK} />);
  await openChip();
  await choose("Provisions memo.md");
  expect(chip()?.textContent).toBe("Provisions memo.md");
  expect(rpc.chats.setMessageArtifact).toHaveBeenCalledWith({
    botId: "bot-1",
    itemId: "item_1",
    artifactId: "art_memo",
  });
  await act(async () =>
    answer({
      id: "item_1",
      artifact: {
        artifactId: "art_memo",
        name: "Provisions memo.md",
        version: null,
        how: "chosen",
      },
    }),
  );
  expect(chip()?.textContent).toBe("Provisions memo.md");
  expect(document.querySelector('[data-testid="message-artifact-failed"]')).toBeNull();
});

it("reverts with a quiet note when the change fails", async () => {
  rpc.chats.setMessageArtifact.mockRejectedValue(new Error("404"));
  await mount(<MessageArtifactChip botId="bot-1" itemId="item_1" artifact={LINK} />);
  await openChip();
  await choose("Provisions memo.md");
  await settle();
  expect(chip()?.textContent).toBe("Q3 Lending Review.html");
  expect(document.querySelector('[data-testid="message-artifact-failed"]')?.textContent).toBe(
    "Couldn't change",
  );
});

it("None removes the chip", async () => {
  rpc.chats.setMessageArtifact.mockResolvedValue({ id: "item_1", artifact: null });
  await mount(<MessageArtifactChip botId="bot-1" itemId="item_1" artifact={LINK} />);
  await openChip();
  await choose("None");
  expect(chip()).toBeNull();
  expect(rpc.chats.setMessageArtifact).toHaveBeenCalledWith({
    botId: "bot-1",
    itemId: "item_1",
    artifactId: null,
  });
});

it("a message not yet recorded shows the chip without a menu", async () => {
  await mount(<MessageArtifactChip botId="bot-1" itemId="optimistic" artifact={LINK} />);
  expect(chip()?.textContent).toBe("Q3 Lending Review.html");
  expect(chip()?.getAttribute("role")).toBeNull();
});
