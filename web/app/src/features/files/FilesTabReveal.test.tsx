// @vitest-environment jsdom

import type { ComponentProps, ReactNode } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../test/i18n")).withI18nRoot(await orig<typeof import("react-dom/client")>()),
);

const api = vi.hoisted(() => ({ list: vi.fn() }));
vi.mock("../../lib/rpc", () => ({ rpc: { files: api } }));
vi.mock("../../lib/relative-time", () => ({ formatRelativeTime: () => "just now" }));
vi.mock("./FilePreviewDialog", () => ({ FilePreviewDialog: () => null }));
vi.mock("@orpc/client", () => ({ ORPCError: class extends Error {} }));
vi.mock("@lingui/react/macro", () => {
  const t = (parts: TemplateStringsArray, ...values: unknown[]) =>
    parts.reduce((acc, part, i) => `${acc}${part}${values[i] ?? ""}`, "");
  return { useLingui: () => ({ t }), Trans: ({ children }: { children: ReactNode }) => children };
});
vi.mock("@superchat/ui-web", () => ({
  Input: (props: ComponentProps<"input">) => <input {...props} />,
  cn: (...classes: unknown[]) => classes.filter(Boolean).join(" "),
}));

import { FilesTab } from "./FilesTab";

const entry = (name: string, type: "file" | "directory", path = name) => ({
  name,
  path,
  type,
  size: type === "file" ? 10 : null,
  modifiedAt: 1,
});

it("opens a revealed Project folder and its parents, and again on a new request", async () => {
  api.list.mockImplementation(async ({ path }: { path: string }) => ({
    entries:
      path === ""
        ? [entry("projects", "directory"), entry("notes.txt", "file")]
        : path === "projects"
          ? [entry("q3-deck", "directory", "projects/q3-deck")]
          : [entry("PROJECT.md", "file", "projects/q3-deck/PROJECT.md")],
  }));
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(<FilesTab botId="bot-1" refreshKey={0} />));
  expect(container.textContent).not.toContain("PROJECT.md");

  const reveal = { path: "projects/q3-deck", nonce: 1 };
  await act(async () => root.render(<FilesTab botId="bot-1" refreshKey={0} reveal={reveal} />));
  expect(container.textContent).toContain("PROJECT.md");
  expect(api.list).toHaveBeenCalledWith({ botId: "bot-1", path: "projects/q3-deck" });

  // Collapse "projects", then ask again: the same Project opens once more.
  const rows = () => [...container.querySelectorAll<HTMLButtonElement>("[data-testid=file-row]")];
  await act(async () => rows()[0]?.click());
  expect(container.textContent).not.toContain("PROJECT.md");
  await act(async () =>
    root.render(<FilesTab botId="bot-1" refreshKey={0} reveal={{ ...reveal, nonce: 2 }} />),
  );
  expect(container.textContent).toContain("PROJECT.md");
  await act(async () => root.unmount());
});
