import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./client", () => ({
  engineClient: () => ({ baseUrl: "http://engine.test", proxySecret: "", secrets: [] }),
  museSessionId: async () => "muse-1",
  NO_EMAIL: "",
}));

const { engineModelsRouter } = await import("./models");
const { call } = await import("@orpc/server");

function stubEngine(...responses: { status?: number; body: unknown }[]) {
  const fetchMock = vi.fn();
  for (const { status = 200, body } of responses) {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      }),
    );
  }
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const setHarness = (harness: "claude-sdk" | "pi") =>
  call(engineModelsRouter.engineModels.setHarness, { harness });

afterEach(() => vi.unstubAllGlobals());

describe("setHarness", () => {
  it("runs the Superchat on the harness's built-in agent", async () => {
    const fetchMock = stubEngine({
      body: { session_id: "muse-1", agent: "superchat-pi", created: false },
    });
    expect(await setHarness("pi")).toEqual({ ok: true });
    const [url, init] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect(url.pathname).toBe("/v1/me/muse");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(String(init.body))).toEqual({ agent: "superchat-pi" });
  });

  it("asks the person to finish the reply while one runs", async () => {
    stubEngine({
      status: 409,
      body: { error: { code: "conflict", message: "Session is busy" } },
    });
    await expect(setHarness("claude-sdk")).rejects.toMatchObject({
      code: "CONFLICT",
      message: "Finish or stop the current reply first.",
      data: { code: "conflict" },
    });
  });
});

describe("status", () => {
  it("names the one harness the Superchat runs on", async () => {
    stubEngine(
      { body: { session_id: "muse-1", agent: "superchat-pi", created: false } },
      { body: { harness: "pi", status: "ready", default_model: null, models: [] } },
    );
    expect(await call(engineModelsRouter.engineModels.status, undefined)).toEqual({
      enabled: true,
      harnesses: [{ id: "pi", label: "Pi" }],
      ready: true,
    });
  });
});

describe("setPick", () => {
  it("replaces one harness's pick in one category and keeps the rest", async () => {
    const fetchMock = stubEngine(
      {
        body: {
          foundation: { pi: "a", "claude-sdk": "b" },
          builder: {},
          light: { pi: "c" },
          defaults: { pi: "a", "claude-sdk": "b" },
        },
      },
      { body: { foundation: { pi: "z", "claude-sdk": "b" }, builder: {}, light: { pi: "c" } } },
    );
    await call(engineModelsRouter.engineModels.setPick, {
      harness: "pi",
      category: "foundation",
      model: "z",
    });
    const [, init] = fetchMock.mock.calls[1] as [URL, RequestInit];
    expect(init.method).toBe("PUT");
    expect(JSON.parse(String(init.body))).toEqual({ foundation: { pi: "z", "claude-sdk": "b" } });
  });

  it("goes back to automatic by leaving the harness out", async () => {
    const fetchMock = stubEngine(
      { body: { foundation: {}, builder: {}, light: { pi: "c", "claude-sdk": "d" } } },
      { body: { foundation: {}, builder: {}, light: { "claude-sdk": "d" } } },
    );
    await call(engineModelsRouter.engineModels.setPick, {
      harness: "pi",
      category: "light",
      model: null,
    });
    const [, init] = fetchMock.mock.calls[1] as [URL, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({ light: { "claude-sdk": "d" } });
  });
});

describe("catalog", () => {
  it("carries the three picks", async () => {
    stubEngine({
      body: {
        harness: "pi",
        status: "ready",
        provider_label: "Anthropic",
        default_model: "m2",
        foundation_model: "m1",
        builder_model: "m1",
        light_model: "m2",
        picks: { foundation: "m1", builder: null, light: null },
        automatic: { foundation: "m1", builder: "m1", light: "m2" },
        models: [
          { id: "m1", label: "M1", is_default: false, categories: ["foundation", "builder"] },
          { id: "m2", label: "M2", is_default: true, categories: ["light"] },
        ],
      },
    });
    const catalog = await call(engineModelsRouter.engineModels.catalog, { harness: "pi" });
    expect(catalog.foundationModel).toBe("m1");
    expect(catalog.picks).toEqual({ foundation: "m1", builder: null, light: null });
    expect(catalog.models.map((m) => m.id)).toEqual(["m1", "m2"]);
  });
});
