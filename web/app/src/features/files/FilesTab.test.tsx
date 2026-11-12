// @vitest-environment jsdom

import { i18n } from "@lingui/core";
import { I18nProvider } from "@lingui/react";
import type { ComponentProps } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ list: vi.fn() }));
vi.mock("../../lib/rpc", () => ({ rpc: { files: api } }));
vi.mock("../../lib/relative-time", () => ({ formatRelativeTime: () => "just now" }));
vi.mock("./FilePreviewDialog", () => ({
  FilePreviewDialog: ({ path }: { path: string }) => <div data-testid="preview">{path}</div>,
}));
vi.mock("@orpc/client", () => ({
  ORPCError: class extends Error {
    code = "SERVICE_UNAVAILABLE";
  },
}));
vi.mock("@superchat/ui-web", () => ({
  Input: (props: ComponentProps<"input">) => <input {...props} />,
  cn: (...classes: unknown[]) => classes.filter(Boolean).join(" "),
}));

import { FilesTab as Tab } from "./FilesTab";

i18n.loadAndActivate({ locale: "en", messages: {} });
const FilesTab = (props: { botId: string; refreshKey: number }) => (
  <I18nProvider i18n={i18n}>
    <Tab {...props} />
  </I18nProvider>
);

const entry = (name: string, type: "file" | "directory", path = name, modifiedAt = 1) => ({
  name,
  path,
  type,
  size: type === "file" ? 2048 : null,
  modifiedAt,
});

it("expands a folder lazily and opens a file in the preview", async () => {
  api.list.mockImplementation(async ({ path }: { path: string }) => ({
    entries:
      path === ""
        ? [entry("reports", "directory"), entry("notes.txt", "file")]
        : [entry("bank.html", "file", "reports/bank.html")],
  }));
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(<FilesTab botId="bot-1" refreshKey={0} />));

  expect(api.list).toHaveBeenCalledTimes(1);
  expect(container.textContent).toContain("notes.txt");
  expect(container.textContent).toContain("2 KB");
  expect(container.textContent).not.toContain("bank.html");

  const rows = () => [...container.querySelectorAll<HTMLButtonElement>("[data-testid=file-row]")];
  await act(async () => rows()[0]?.click());
  expect(api.list).toHaveBeenLastCalledWith({ botId: "bot-1", path: "reports" });
  expect(container.textContent).toContain("bank.html");

  await act(async () => rows()[1]?.click());
  expect(container.querySelector("[data-testid=preview]")?.textContent).toBe("reports/bank.html");

  await act(async () => root.render(<FilesTab botId="bot-1" refreshKey={1} />));
  expect(api.list).toHaveBeenCalledTimes(4);
  await act(async () => root.unmount());
});

it("hides dotfiles until toggled", async () => {
  api.list.mockReset();
  api.list.mockResolvedValue({ entries: [entry(".env", "file"), entry("notes.txt", "file")] });
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(<FilesTab botId="bot-2" refreshKey={0} />));
  expect(container.textContent).not.toContain(".env");
  await act(async () =>
    container.querySelector<HTMLButtonElement>("[aria-label='Show hidden files']")?.click(),
  );
  expect(container.textContent).toContain(".env");
  await act(async () => root.unmount());
});

it("shows a quiet starting state and retries until the host answers", async () => {
  vi.useFakeTimers();
  const { ORPCError } = await import("@orpc/client");
  api.list.mockReset();
  api.list
    .mockRejectedValueOnce(new (ORPCError as unknown as new () => Error)())
    .mockResolvedValue({ entries: [entry("notes.txt", "file")] });
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(<FilesTab botId="bot-3" refreshKey={0} />));
  expect(container.textContent).toContain("Starting your machine");
  expect(container.textContent).not.toContain("Could not load");
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2600);
  });
  expect(container.textContent).toContain("notes.txt");
  await act(async () => root.unmount());
  vi.useRealTimers();
});
