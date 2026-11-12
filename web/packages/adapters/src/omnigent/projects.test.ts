import { afterEach, describe, expect, it, vi } from "vitest";
import {
  getOmnigentWorkingPlace,
  getOmnigentWorkingProject,
  getOmnigentWorkspaceRoot,
  moveOmnigentSessionForPerson,
} from "./projects.js";

const CONFIG = { baseUrl: "http://omnigent.test", proxySecret: "proxy-secret", tenant: "space-1" };
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

afterEach(() => vi.unstubAllGlobals());

describe("getOmnigentWorkingProject", () => {
  it("reads the Project from the session's Super Chat block", async () => {
    const fetchMock = vi.fn(async () =>
      json({
        id: "conv_1",
        status: "idle",
        superchat: { kind: "super", project: { slug: "q3-deck", name: "Q3 board deck" } },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await expect(getOmnigentWorkingProject(CONFIG, "a@b.c", "conv_1")).resolves.toEqual({
      slug: "q3-deck",
      name: "Q3 board deck",
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("is null with no Project open, or outside a Super Chat family", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json({ id: "conv_1", status: "idle", superchat: { kind: "super", project: null } }),
      ),
    );
    await expect(getOmnigentWorkingProject(CONFIG, "a@b.c", "conv_1")).resolves.toBeNull();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ id: "conv_1", status: "idle", superchat: null })),
    );
    await expect(getOmnigentWorkingProject(CONFIG, "a@b.c", "conv_1")).resolves.toBeNull();
  });
});

describe("getOmnigentWorkingPlace", () => {
  it("reads the folder and the person's mark", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json({
          id: "conv_1",
          status: "idle",
          superchat: { kind: "super", project: null, folder: "reports", workspace_chosen: true },
        }),
      ),
    );
    await expect(getOmnigentWorkingPlace(CONFIG, "a@b.c", "conv_1")).resolves.toEqual({
      project: null,
      folder: "reports",
      chosen: true,
    });
  });
});

describe("getOmnigentWorkspaceRoot", () => {
  it("is the working directory minus the folder the session is in", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json({
          id: "conv_1",
          status: "idle",
          workspace: "/home/u/workspace/projects/q3/",
          superchat: { kind: "super", project: null, folder: "projects/q3" },
        }),
      ),
    );
    await expect(getOmnigentWorkspaceRoot(CONFIG, "a@b.c", "conv_1")).resolves.toBe(
      "/home/u/workspace",
    );
  });
});

describe("moveOmnigentSessionForPerson", () => {
  it("moves the session, then marks the choice and the one-shot move", async () => {
    const fetchMock = vi.fn(async (_url: URL, init?: RequestInit) =>
      init?.method === "PUT" ? json({ workspace: "/home/u/workspace/reports" }) : json({}),
    );
    vi.stubGlobal("fetch", fetchMock);
    await moveOmnigentSessionForPerson(
      CONFIG,
      "a@b.c",
      "conv_1",
      "/home/u/workspace/reports",
      true,
    );
    const [put, patch] = fetchMock.mock.calls;
    expect(String(put?.[0])).toBe("http://omnigent.test/v1/sessions/conv_1/workspace");
    expect(patch?.[1]?.method).toBe("PATCH");
    expect(JSON.parse(String(patch?.[1]?.body))).toEqual({
      labels: {
        "omnigent.superchat.workspace.chosen": "person",
        "omnigent.superchat.workspace.moved": "/home/u/workspace/reports",
      },
    });
  });

  it("clears the mark when the person goes back to the whole workspace", async () => {
    const fetchMock = vi.fn(async (_url: URL, init?: RequestInit) =>
      init?.method === "PUT" ? json({ workspace: "/home/u/workspace" }) : json({}),
    );
    vi.stubGlobal("fetch", fetchMock);
    await moveOmnigentSessionForPerson(CONFIG, "a@b.c", "conv_1", "/home/u/workspace", false);
    const body = JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body));
    expect(body.labels["omnigent.superchat.workspace.chosen"]).toBe("");
  });
});
