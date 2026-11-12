// @vitest-environment jsdom

import type { KnowledgeFile, KnowledgeStatus } from "@superchat/contracts";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const status = vi.fn();
const reindex = vi.fn();
vi.mock("../../lib/rpc", () => ({
  rpc: {
    knowledge: {
      status: (...a: unknown[]) => status(...a),
      reindex: (...a: unknown[]) => reindex(...a),
    },
  },
}));

import { resetKnowledgeStatus, useKnowledgeFile } from "./status";

const file = (id: string, over: Partial<KnowledgeFile> = {}): KnowledgeFile => ({
  fileId: `f-${id}`,
  path: `your_files/uploads/2026-10-09/${id}.pdf`,
  artifactId: id,
  name: `${id}.pdf`,
  kind: "pdf",
  state: "searchable",
  search: "hybrid",
  keywordOnlyReason: null,
  pages: 3,
  pagesWithoutText: 0,
  error: null,
  ...over,
});
const answer = (
  files: KnowledgeFile[],
  available = true,
  host: "awake" | "asleep" = "awake",
): KnowledgeStatus => ({
  files,
  embeddings: { available, reason: available ? null : "no_connection" },
  host,
  updatedAt: 1,
});

const hosts: HTMLElement[] = [];
function Probe({ id, seen }: { id: string; seen: (file: KnowledgeFile | undefined) => void }) {
  seen(useKnowledgeFile(id));
  return null;
}
async function mount(id: string, seen: (file: KnowledgeFile | undefined) => void) {
  const host = document.createElement("div");
  hosts.push(host);
  const root = createRoot(host);
  await act(async () => root.render(createElement(Probe, { id, seen })));
  return root;
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  status.mockReset();
  reindex.mockReset().mockResolvedValue({ queued: 0 });
});
afterEach(() => {
  resetKnowledgeStatus();
  vi.useRealTimers();
});

it("one poll serves every badge, and a file still being read is polled again soon", async () => {
  status
    .mockResolvedValueOnce(answer([file("a", { state: "indexing" }), file("b")]))
    .mockResolvedValue(answer([file("a"), file("b")]));
  const seenA = vi.fn();
  const seenB = vi.fn();
  await mount("a", seenA);
  await mount("b", seenB);
  expect(status).toHaveBeenCalledTimes(1);
  expect(seenA).toHaveBeenLastCalledWith(expect.objectContaining({ state: "indexing" }));
  expect(seenB).toHaveBeenLastCalledWith(expect.objectContaining({ state: "searchable" }));
  await act(async () => vi.advanceTimersByTimeAsync(3_100));
  expect(status).toHaveBeenCalledTimes(2);
  expect(seenA).toHaveBeenLastCalledWith(expect.objectContaining({ state: "searchable" }));
});

it("settles to a slow poll once nothing is being read, and stops with the last badge", async () => {
  status.mockResolvedValue(answer([file("a")]));
  const root = await mount("a", vi.fn());
  await act(async () => vi.advanceTimersByTimeAsync(10_000));
  expect(status).toHaveBeenCalledTimes(1);
  await act(async () => vi.advanceTimersByTimeAsync(11_000));
  expect(status).toHaveBeenCalledTimes(2);
  await act(async () => root.unmount());
  await act(async () => vi.advanceTimersByTimeAsync(120_000));
  expect(status).toHaveBeenCalledTimes(2);
});

it("a file the engine does not list yet is looked up again", async () => {
  status.mockResolvedValueOnce(answer([])).mockResolvedValue(answer([file("new")]));
  const seen = vi.fn();
  await mount("new", seen);
  expect(seen).toHaveBeenLastCalledWith(undefined);
  await act(async () => vi.advanceTimersByTimeAsync(4_100));
  await act(async () => vi.advanceTimersByTimeAsync(0));
  expect(seen).toHaveBeenLastCalledWith(expect.objectContaining({ artifactId: "new" }));
});

it("a failing engine keeps the last answer and does not throw", async () => {
  status.mockResolvedValueOnce(answer([file("a")])).mockRejectedValue(new Error("down"));
  const seen = vi.fn();
  await mount("a", seen);
  await act(async () => vi.advanceTimersByTimeAsync(25_000));
  expect(seen).toHaveBeenLastCalledWith(expect.objectContaining({ artifactId: "a" }));
});

it("asks the engine once to embed keyword-only files when the person can embed now", async () => {
  status.mockResolvedValue(answer([file("a", { search: "keyword" })], true));
  await mount("a", vi.fn());
  await act(async () => vi.advanceTimersByTimeAsync(25_000));
  expect(reindex).toHaveBeenCalledTimes(1);
});

it("does not ask to embed while the person still has no connection", async () => {
  status.mockResolvedValue(answer([file("a", { search: "keyword" })], false));
  await mount("a", vi.fn());
  expect(reindex).not.toHaveBeenCalled();
});

it("an asleep host is polled slowly and never asked to reindex", async () => {
  status.mockResolvedValue(
    answer([file("a", { state: "indexing", search: "keyword" })], true, "asleep"),
  );
  await mount("a", vi.fn());
  await act(async () => vi.advanceTimersByTimeAsync(50_000));
  expect(status).toHaveBeenCalledTimes(1);
  await act(async () => vi.advanceTimersByTimeAsync(11_000));
  expect(status).toHaveBeenCalledTimes(2);
  expect(reindex).not.toHaveBeenCalled();
});

it("a Library card finds its file by artifact id, not by path", async () => {
  status.mockResolvedValue(answer([file("a"), file("b", { artifactId: null })]));
  const seen = vi.fn();
  await mount("a", seen);
  expect(seen).toHaveBeenLastCalledWith(expect.objectContaining({ fileId: "f-a" }));
});
