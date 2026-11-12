// @vitest-environment jsdom
// A studio turn changing the file (ADR 0024) reaches the HTML editor as `locked`: nothing staged
// by hand is saved until it ends.

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

vi.mock("../../../../lib/rpc", () => ({ rpc: { artifacts: { edit: vi.fn() } } }));

import { type HtmlEditSource, useHtmlEditor } from "./useHtmlEditor";

const source = vi.fn<HtmlEditSource>(async () => ({ id: "v2", version: 2 }));
let editor: ReturnType<typeof useHtmlEditor>;

function Harness({ locked }: { locked: boolean }) {
  editor = useHtmlEditor({
    fileKey: "bot:deck.deck.html",
    active: true,
    artifactId: "v1",
    version: 1,
    html: "<p>x</p>",
    post: () => {},
    locked,
    source,
  });
  return null;
}

const roots: Root[] = [];
beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  source.mockClear();
});
afterEach(async () => {
  for (const root of roots.splice(0)) await act(async () => root.unmount());
});

async function mount(locked: boolean) {
  const root = createRoot(document.createElement("div"));
  roots.push(root);
  await act(async () => root.render(<Harness locked={locked} />));
  return root;
}

it("saves nothing while locked, and saves again once the turn ends", async () => {
  const root = await mount(true);
  await act(async () => {
    await editor.saveSource("<p>y</p>");
  });
  expect(source).not.toHaveBeenCalled();
  await act(async () => root.render(<Harness locked={false} />));
  await act(async () => {
    await editor.saveSource("<p>y</p>");
  });
  expect(source).toHaveBeenCalledTimes(1);
});
