// @vitest-environment jsdom

import { call } from "@orpc/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { artifactsRouter } from "./artifacts";
import { forgetMuseSession } from "./client";

const row = {
  id: "a1",
  parent_session_id: "sess-1",
  name: "report.html",
  title: "Report",
  kind: "html",
  mime: "text/html; charset=utf-8",
  size: 12,
  version: 2,
  versions: 2,
  published: false,
  created_at: 1_700_000_000,
  updated_at: null,
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

/** A session in the Muse's family, as `GET /v1/sessions/{id}` reports it. */
const family = (id: string, kind: "helper") => ({
  id,
  status: "idle",
  superchat: { kind, root_id: "sess-1", parent_id: "sess-1", project: null },
});

/** Serves the Muse (session `sess-1`) and hands everything else to `rest`. */
function engine(rest: (url: URL, init?: RequestInit) => Response | Promise<Response>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: URL | string, init?: RequestInit) => {
      const url = new URL(String(input));
      if (url.pathname === "/v1/me/muse") return json({ session_id: "sess-1" });
      return rest(url, init);
    }),
  );
}

const listSpace = (input: { botId?: string; limit?: number } = {}) =>
  call(artifactsRouter.artifacts.listSpace, input);

beforeEach(() => forgetMuseSession());
afterEach(() => vi.unstubAllGlobals());

describe("artifacts", () => {
  it("lists the Muse's deliverables in the Library's shape", async () => {
    engine((url) => {
      expect(url.pathname).toBe("/v1/artifacts");
      expect(url.searchParams.get("parent_session_id")).toBeNull();
      return json({ artifacts: [row] });
    });
    const page = await listSpace();
    expect(page.nextCursor).toBeNull();
    expect(page.items[0]).toMatchObject({
      id: "a1",
      botId: "sess-1",
      name: "report.html",
      description: "Report",
      mimeType: "text/html",
      version: 2,
      versionCount: 2,
    });
  });

  it("lists deliverables saved by a Helper under the Muse", async () => {
    const helperSaved = { ...row, id: "a3", parent_session_id: "helper-1", name: "h.html" };
    const stranger = { ...row, id: "a4", parent_session_id: "elsewhere", name: "x.html" };
    engine((url) => {
      if (url.pathname === "/v1/artifacts") {
        return json({ artifacts: [row, helperSaved, stranger] });
      }
      if (url.pathname === "/v1/sessions/helper-1") return json(family("helper-1", "helper"));
      return json({ id: "elsewhere", status: "idle", superchat: null });
    });
    const page = await call(artifactsRouter.artifacts.list, { botId: "sess-1" });
    expect(page.map((item) => item.id).sort()).toEqual(["a1", "a3"]);
    expect(page.every((item) => item.botId === "sess-1")).toBe(true);
  });

  it("names the chat and origin of each newest version", async () => {
    const helperSaved = { ...row, id: "a3", name: "h.html", origin: "ai", chat_id: "helper-1" };
    engine(() => json({ artifacts: [helperSaved] }));
    const [item] = await call(artifactsRouter.artifacts.list, { botId: "sess-1" });
    expect(item).toMatchObject({ id: "a3", origin: "ai", chatId: "helper-1" });
  });

  it("resolves each chat's owner from its session once", async () => {
    const rows = Array.from({ length: 6 }, (_, i) => ({
      ...row,
      id: `h${i}`,
      parent_session_id: i % 2 ? "helper-1" : "helper-2",
    }));
    const calls: string[] = [];
    engine((url) => {
      calls.push(url.pathname);
      if (url.pathname === "/v1/artifacts") return json({ artifacts: rows });
      return json(family(url.pathname.split("/").at(-1) ?? "", "helper"));
    });
    const page = await listSpace();
    expect(page.items).toHaveLength(6);
    expect(calls.filter((c) => c === "/v1/sessions/helper-1")).toHaveLength(1);
    expect(calls.filter((c) => c === "/v1/sessions/helper-2")).toHaveLength(1);
  });

  it("honors the requested limit", async () => {
    engine(() => json({ artifacts: [row, { ...row, id: "a2" }, { ...row, id: "a3" }] }));
    const page = await listSpace({ limit: 2 });
    expect(page.items.map((item) => item.id)).toEqual(["a1", "a2"]);
  });

  it("surfaces engine failures instead of dropping rows", async () => {
    engine((url) =>
      url.pathname === "/v1/artifacts"
        ? json({ artifacts: [{ ...row, parent_session_id: "unknown" }] })
        : json({ error: "boom" }, 500),
    );
    await expect(listSpace()).rejects.toThrow();
  });

  it("returns versions, content as base64, and deletes", async () => {
    const detail = {
      ...row,
      all_versions: [
        { id: "a2", version: 3, size: 12, created_at: 1_700_000_200, item_id: "msg_7" },
        {
          id: "a1",
          version: 2,
          size: 12,
          created_at: 1_700_000_100,
          origin: "ai",
          chat_id: "sess-1",
          turn_item_id: "msg_5",
        },
        { id: "a0", version: 1, size: 10, created_at: 1_700_000_000, origin: "manual" },
      ],
    };
    engine((url, init) => {
      if (init?.method === "DELETE") return json({ deleted: true });
      if (url.pathname.endsWith("/content")) return new Response("<h1>hi</h1>");
      return json(detail);
    });
    const versions = await call(artifactsRouter.artifacts.listVersions, { familyId: "a1" });
    // The message each came from: the engine's `item_id`, else the Conversation's own turn item.
    expect(versions.map((v) => [v.id, v.version, v.itemId])).toEqual([
      ["a2", 3, "msg_7"],
      ["a1", 2, "msg_5"],
      ["a0", 1, null],
    ]);
    const full = await call(artifactsRouter.artifacts.getById, { artifactId: "a1" });
    expect(atob(full.contentBase64)).toBe("<h1>hi</h1>");
    expect(full.botId).toBe("sess-1");
    expect(await call(artifactsRouter.artifacts.remove, { artifactId: "a1" })).toEqual({
      ok: true,
    });
  });

  it("restores an earlier version as a new one", async () => {
    let posted = "";
    engine((url, init) => {
      posted = `${init?.method} ${url.pathname}`;
      return json({ ...row, id: "a3", version: 3, origin: "restore" });
    });
    const created = await call(artifactsRouter.artifacts.restore, { artifactId: "a0" });
    expect(posted).toBe("POST /v1/artifacts/a0/restore");
    expect(created).toMatchObject({ id: "a3", version: 3, botId: "sess-1" });
  });

  it("maps an engine 404 to not-found", async () => {
    engine(() => json({ error: { code: "not_found" } }, 404));
    await expect(
      call(artifactsRouter.artifacts.getById, { artifactId: "zz" }),
    ).rejects.toMatchObject({ code: "NOT_FOUND" });
  });
});

describe("artifact edit", () => {
  const patches = [{ kind: "set-text" as const, id: "title", text: "B" }];
  const edit = (input: { patches: unknown[] } = { patches }) =>
    call(artifactsRouter.artifacts.edit, { artifactId: "a1", baseVersion: 1, ...input } as never);

  it("patches and returns the new manual version", async () => {
    const fetchMock = vi.fn(async (url: URL, init?: RequestInit) => {
      expect(url.pathname).toBe("/v1/artifacts/a1/edit");
      expect(init?.method).toBe("PATCH");
      return json({ ...row, id: "a3", version: 2 });
    });
    vi.stubGlobal("fetch", fetchMock);
    expect(await edit()).toMatchObject({ id: "a3" });
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      base_version: 1,
      patches,
    });
  });

  it("maps a stale base and an inexact patch to CONFLICT, telling them apart by code", async () => {
    const refuse = (code: string) =>
      vi.stubGlobal(
        "fetch",
        vi.fn(async () => json({ error: { code, message: "refused" } }, 409)),
      );
    refuse("artifact_stale");
    await expect(edit()).rejects.toMatchObject({
      code: "CONFLICT",
      data: { code: "artifact_stale" },
    });
    refuse("conflict");
    await expect(edit()).rejects.toMatchObject({ code: "CONFLICT", data: { code: "conflict" } });
  });

  it("sends a set-theme patch as an ordinary edit", async () => {
    const themePatches = [{ kind: "set-theme" as const, theme: "nord" }];
    const fetchMock = vi.fn(async () => json({ ...row, id: "a3", version: 2 }));
    vi.stubGlobal("fetch", fetchMock);
    await edit({ patches: themePatches });
    const init = (fetchMock.mock.calls as unknown as [URL, RequestInit][])[0]?.[1];
    expect(JSON.parse(String(init?.body)).patches).toEqual(themePatches);
  });
});
