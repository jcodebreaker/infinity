// @vitest-environment jsdom

import JSZip from "jszip";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const renderer = vi.hoisted(() => ({ open: vi.fn(), destroy: vi.fn() }));
vi.mock("@aiden0z/pptx-renderer", () => ({
  PptxViewer: { open: renderer.open },
  RECOMMENDED_ZIP_LIMITS: { maxEntries: 1 },
}));

import { PptxPreview } from "./PptxPreview";

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  renderer.open.mockResolvedValue({ destroy: renderer.destroy });
});
afterEach(() => {
  renderer.open.mockReset();
  renderer.destroy.mockReset();
  document.body.innerHTML = "";
});

async function deck(): Promise<Uint8Array> {
  const zip = new JSZip();
  zip.file("ppt/presentation.xml", "<p/>");
  return zip.generateAsync({ type: "uint8array" });
}

const settle = () => act(async () => new Promise((resolve) => setTimeout(resolve, 20)));

it("opens the deck in its container, fitted to the panel, and destroys it on close", async () => {
  const bytes = await deck();
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(<PptxPreview bytes={bytes} fallback={<p>fallback</p>} />));
  await settle();
  expect(renderer.open).toHaveBeenCalledTimes(1);
  const [source, host, options] = renderer.open.mock.calls[0] ?? [];
  expect(source).toBe(bytes);
  expect(host).toBe(container.querySelector('[data-testid="pptx-preview"]'));
  expect(options).toMatchObject({ fitMode: "contain", zipLimits: { maxEntries: 1 } });
  expect(container.querySelector('[data-testid="pptx-preview"]')?.getAttribute("aria-busy")).toBe(
    "false",
  );
  await act(async () => root.unmount());
  expect(renderer.destroy).toHaveBeenCalledTimes(1);
});

it("shows the caller's fallback when the file cannot be read", async () => {
  renderer.open.mockRejectedValue(new Error("not a presentation"));
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () =>
    root.render(<PptxPreview bytes={await deck()} fallback={<p>fallback</p>} />),
  );
  await settle();
  expect(container.textContent).toBe("fallback");
  expect(container.querySelector('[data-testid="pptx-preview"]')).toBeNull();
});

it("does not keep a viewer that finished opening after the panel closed", async () => {
  let finish: (viewer: { destroy: () => void }) => void = () => {};
  renderer.open.mockReturnValue(new Promise((resolve) => (finish = resolve)));
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(<PptxPreview bytes={await deck()} fallback={null} />));
  await settle();
  await act(async () => root.unmount());
  await act(async () => finish({ destroy: renderer.destroy }));
  await settle();
  expect(renderer.destroy).toHaveBeenCalledTimes(1);
});
