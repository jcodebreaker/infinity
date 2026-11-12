// @vitest-environment jsdom

import type { KnowledgeFile } from "@superchat/contracts";
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
vi.mock("@superchat/ui-web", () => ({
  cn: (...classes: unknown[]) => classes.filter(Boolean).join(" "),
}));
const status = vi.fn();
vi.mock("../../lib/rpc", () => ({
  rpc: { knowledge: { status: (...a: unknown[]) => status(...a), reindex: vi.fn() } },
}));

import { knowledgeExtension } from "./extension";
import { IndexBadge } from "./IndexBadge";
import { resetKnowledgeStatus } from "./status";

const file = (over: Partial<KnowledgeFile>): KnowledgeFile => ({
  fileId: "f-1",
  path: "your_files/uploads/2026-10-09/report.pdf",
  artifactId: "a-1",
  name: "report.pdf",
  kind: "pdf",
  state: "searchable",
  search: "hybrid",
  keywordOnlyReason: null,
  pages: 3,
  pagesWithoutText: 0,
  error: null,
  ...over,
});

const hosts: HTMLElement[] = [];
async function mount(node: ReactNode) {
  const host = document.createElement("div");
  document.body.append(host);
  hosts.push(host);
  await act(async () => createRoot(host).render(node));
  return host;
}

beforeEach(() => vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true));
afterEach(() => {
  resetKnowledgeStatus();
  for (const host of hosts.splice(0)) host.remove();
  status.mockReset();
});

it.each([
  ["indexing", "Indexing…"],
  ["searchable", "Searchable"],
  ["failed", "Couldn't index"],
] as const)("shows %s as %s", async (state, text) => {
  status.mockResolvedValue({
    files: [file({ state })],
    embeddings: { available: true, reason: null },
    host: "awake",
    updatedAt: 1,
  });
  const host = await mount(<IndexBadge artifactId="a-1" />);
  const badge = host.querySelector("[data-testid=library-card-index]");
  expect(badge?.textContent).toBe(text);
  expect(badge?.getAttribute("data-state")).toBe(state);
});

it("pulses while indexing, but shows the last-known state calmly when the host sleeps", async () => {
  const answer = (host: "awake" | "asleep") => ({
    files: [file({ state: "indexing" })],
    embeddings: { available: true, reason: null },
    host,
    updatedAt: 1,
  });
  status.mockResolvedValue(answer("awake"));
  const awake = await mount(<IndexBadge artifactId="a-1" />);
  expect(awake.querySelector("[aria-hidden]")?.className).toContain("animate-pulse");
  resetKnowledgeStatus();
  status.mockResolvedValue(answer("asleep"));
  const asleep = await mount(<IndexBadge artifactId="a-1" />);
  expect(asleep.querySelector("[data-state=indexing]")).not.toBeNull();
  expect(asleep.querySelector("[aria-hidden]")?.className).not.toContain("animate-pulse");
});

it("shows nothing for a file the index does not hold", async () => {
  status.mockResolvedValue({
    files: [],
    embeddings: { available: true, reason: null },
    host: "awake",
    updatedAt: 1,
  });
  const host = await mount(<IndexBadge artifactId="a-1" />);
  expect(host.querySelector("[data-testid=library-card-index]")).toBeNull();
});

it("the extension badges indexable Library files and leaves the rest alone", () => {
  const base = { id: "a-1", name: "x", description: null, size: 1, version: 1, createdAt: "" };
  const card = (mimeType: string) => knowledgeExtension.card({ ...base, mimeType } as never);
  for (const mime of [
    "application/pdf",
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ]) {
    expect(card(mime)?.badge).toBeTruthy();
  }
  expect(card("text/html")).toBeNull();
  expect(card("image/png")).toBeNull();
});
