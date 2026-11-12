// @vitest-environment jsdom

import { call } from "@orpc/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { appsRouter } from "./apps";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

afterEach(() => vi.unstubAllGlobals());

describe("apps", () => {
  it("publishes, maps the address and view counts, and unpublishes", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: URL | string, init?: RequestInit) => {
        calls.push(`${init?.method ?? "GET"} ${new URL(String(url)).pathname}`);
        if (init?.method === "DELETE") return json({ unpublished: true });
        expect(JSON.parse(String(init?.body))).toEqual({ audience: "org" });
        return json({
          slug: "report-k3m9xq",
          url_path: "/apps/report-k3m9xq",
          audience: "org",
          version: 2,
          published_at: 1_700_000_000,
          updated_at: null,
          stats: { opens_total: 5, unique_viewers: 3, opens_7d: 4 },
        });
      }),
    );
    await expect(
      call(appsRouter.apps.publish, { artifactId: "a1", audience: "org" }),
    ).resolves.toMatchObject({
      slug: "report-k3m9xq",
      urlPath: "/apps/report-k3m9xq",
      audience: "org",
      stats: { opensTotal: 5, uniqueViewers: 3, opens7d: 4 },
    });
    await expect(call(appsRouter.apps.unpublish, { artifactId: "a1" })).resolves.toEqual({
      ok: true,
    });
    expect(calls).toEqual(["POST /v1/artifacts/a1/publish", "DELETE /v1/artifacts/a1/publish"]);
  });

  it("answers a file the engine will not publish as a bad request", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ error: { code: "invalid_input" } }, 400)),
    );
    await expect(
      call(appsRouter.apps.publish, { artifactId: "a1", audience: "link" }),
    ).rejects.toMatchObject({ code: "BAD_REQUEST" });
  });
});
