import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./client", () => ({
  engineClient: () => ({ baseUrl: "http://engine.test", proxySecret: "", secrets: [] }),
  museSessionId: async () => "muse-1",
  NO_EMAIL: "",
}));

const { hostRouter } = await import("./host");
const { call } = await import("@orpc/server");

function stubEngine(...bodies: unknown[]) {
  const fetchMock = vi.fn();
  for (const body of bodies) {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } }),
    );
  }
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const status = () => call(hostRouter.host.status, { botId: "muse-1" });

afterEach(() => vi.unstubAllGlobals());

describe("host status", () => {
  it("reports status from the engine's host route", async () => {
    const fetchMock = stubEngine({ ready: true, launch: null });
    expect(await status()).toEqual({ botId: "muse-1", state: "running", runnerReady: true });
    expect((fetchMock.mock.calls[0] as [URL])[0].pathname).toBe("/v1/sessions/muse-1/host");
  });

  it("reports the launch stage while the host starts, and drops it once ready", async () => {
    stubEngine(
      { ready: false, launch: { stage: "starting", error: null } },
      { ready: true, launch: null },
    );
    expect((await status()).launch).toEqual({ stage: "starting" });
    expect((await status()).launch).toBeUndefined();
  });

  it("is stopped while the runner is not connected", async () => {
    stubEngine({ ready: false, launch: null });
    expect(await status()).toMatchObject({ state: "stopped", runnerReady: false });
  });
});
