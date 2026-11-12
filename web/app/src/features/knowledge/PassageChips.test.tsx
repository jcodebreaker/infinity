// @vitest-environment jsdom

import type { ReactNode } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../test/i18n")).withI18nRoot(await orig<typeof import("react-dom/client")>()),
);
vi.mock("@lingui/react/macro", () => {
  const t = (parts: TemplateStringsArray, ...values: unknown[]) =>
    parts.reduce((acc, part, i) => `${acc}${part}${values[i] ?? ""}`, "");
  return { useLingui: () => ({ t }) };
});
const pageThumbnail = vi.fn();
vi.mock("../../lib/rpc", () => ({
  rpc: { knowledge: { pageThumbnail: (...a: unknown[]) => pageThumbnail(...a) } },
}));

vi.mock("../files", () => ({
  FilePreviewDialog: ({ botId, path, page }: { botId: string; path: string; page?: number }) => (
    <div data-testid="file-preview">{`${botId}|${path}|${page}`}</div>
  ),
}));

import { FileStudioProvider, ReplyCardBotProvider } from "../../components/cards/context";
import { PassageChips, resetThumbnailCache } from "./PassageChips";

const A = "a".repeat(32);
const B = "b".repeat(32);
const data = {
  items: [
    { fileId: "f1", artifactId: A, name: "finance.pdf", page: 2, hasThumbnail: true },
    { fileId: "f2", artifactId: B, name: "notes.md", page: 1 },
  ],
};

const hosts: HTMLElement[] = [];
async function mount(node: ReactNode) {
  const host = document.createElement("div");
  document.body.append(host);
  hosts.push(host);
  await act(async () => createRoot(host).render(node));
  return host;
}

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  pageThumbnail
    .mockReset()
    .mockResolvedValue({ contentBase64: "UklGRg==", mimeType: "image/webp" });
});
afterEach(() => {
  resetThumbnailCache();
  for (const host of hosts.splice(0)) host.remove();
});

it("draws a thumbnail chip per page, fetching each thumbnail once", async () => {
  const host = await mount(<PassageChips data={data} />);
  const chips = host.querySelectorAll("button");
  expect(chips).toHaveLength(2);
  expect(chips[0]?.textContent).toContain("finance.pdf");
  expect(chips[0]?.textContent).toContain("p. 2");
  expect(chips[0]?.getAttribute("aria-label")).toBe("Open finance.pdf, page 2");
  expect(pageThumbnail).toHaveBeenCalledTimes(1);
  expect(pageThumbnail).toHaveBeenCalledWith({ fileId: "f1", page: 2 });
  expect(chips[0]?.querySelector("img")?.getAttribute("src")).toBe(
    "data:image/webp;base64,UklGRg==",
  );
  // A file without page images gets an icon, not a request.
  expect(chips[1]?.querySelector("img")).toBeNull();
  expect(chips[1]?.querySelector("svg")).not.toBeNull();
});

it("clicking a chip opens the file at that page in the panel", async () => {
  const open = vi.fn();
  const host = await mount(
    <FileStudioProvider value={{ open }}>
      <PassageChips data={data} />
    </FileStudioProvider>,
  );
  await act(async () => host.querySelectorAll("button")[0]?.click());
  expect(open).toHaveBeenCalledWith(A, "finance.pdf", 2);
  await act(async () => host.querySelectorAll("button")[1]?.click());
  expect(open).toHaveBeenLastCalledWith(B, "notes.md", 1);
});

it("chips are inert where no panel exists", async () => {
  const host = await mount(<PassageChips data={data} />);
  expect(host.querySelector("button")?.disabled).toBe(true);
});

it("keeps the placeholder when a thumbnail cannot be fetched", async () => {
  pageThumbnail.mockRejectedValue(new Error("gone"));
  const host = await mount(<PassageChips data={data} />);
  expect(host.querySelector("img")).toBeNull();
  expect(host.querySelectorAll("button")).toHaveLength(2);
});

it("shows a skeleton while the thumbnail loads, then a page icon only if it has none", async () => {
  let fail: (e: Error) => void = () => {};
  pageThumbnail.mockReturnValue(new Promise((_, reject) => (fail = reject)));
  const host = await mount(<PassageChips data={data} />);
  const first = host.querySelector("button");
  expect(first?.querySelector("[data-testid=passage-thumbnail-loading]")).not.toBeNull();
  expect(first?.querySelector("svg")).toBeNull();
  await act(async () => fail(new Error("gone")));
  expect(first?.querySelector("[data-testid=passage-thumbnail-loading]")).toBeNull();
  expect(first?.querySelector("svg")).not.toBeNull();
});

it("a path-only chip opens the host file preview at that page", async () => {
  const open = vi.fn();
  const host = await mount(
    <ReplyCardBotProvider botId="bot-1">
      <FileStudioProvider value={{ open }}>
        <PassageChips
          data={{
            items: [
              {
                fileId: "f9",
                path: "uploads/scan.pdf",
                name: "scan.pdf",
                page: 4,
                hasThumbnail: true,
              },
            ],
          }}
        />
      </FileStudioProvider>
    </ReplyCardBotProvider>,
  );
  expect(pageThumbnail).toHaveBeenCalledWith({ fileId: "f9", page: 4 });
  const chip = host.querySelector("button");
  expect(chip?.getAttribute("aria-label")).toBe("Open scan.pdf, page 4");
  expect(document.querySelector("[data-testid=file-preview]")).toBeNull();
  await act(async () => chip?.click());
  expect(document.querySelector("[data-testid=file-preview]")?.textContent).toBe(
    "bot-1|uploads/scan.pdf|4",
  );
  expect(open).not.toHaveBeenCalled();
});

it("a chip with neither artifact nor path stays a plain chip", async () => {
  const host = await mount(
    <ReplyCardBotProvider botId="bot-1">
      <PassageChips data={{ items: [{ fileId: "f9", name: "scan.pdf", page: 4 }] }} />
    </ReplyCardBotProvider>,
  );
  expect(host.querySelector("button")).toBeNull();
  expect(host.textContent).toContain("scan.pdf");
});
