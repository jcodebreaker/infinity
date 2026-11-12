// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

vi.mock("../lib/use-object-url", () => ({ useObjectUrl: () => "blob:pdf-1" }));

import { PdfViewer } from "./PdfViewer";

async function render(page?: number) {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const host = document.createElement("div");
  document.body.append(host);
  await act(async () =>
    createRoot(host).render(<PdfViewer bytes={new Uint8Array(1)} title="r.pdf" page={page} />),
  );
  return host.querySelector("iframe");
}

it("opens the PDF at the requested page", async () => {
  expect((await render(7))?.getAttribute("src")).toBe("blob:pdf-1#page=7");
});

it("opens at the start without a page", async () => {
  expect((await render())?.getAttribute("src")).toBe("blob:pdf-1");
});
