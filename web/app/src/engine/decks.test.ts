import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./client", () => ({
  engineClient: () => ({ baseUrl: "http://engine.test", proxySecret: "", secrets: [] }),
  museSessionId: async () => "sess-1",
  NO_EMAIL: "",
}));

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const call = (proc: unknown, input?: unknown) =>
  (proc as { callable: () => (input: never) => Promise<unknown> }).callable()(input as never);

afterEach(() => vi.unstubAllGlobals());

const { decksRouter } = await import("./decks");

const row = {
  id: "a2",
  parent_session_id: "sess-1",
  name: "q3.pptx",
  title: "Q3 (PowerPoint)",
  kind: "pptx",
  mime: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  size: 900,
  version: 1,
  versions: 1,
  published: false,
  created_at: 1_700_000_000,
  updated_at: null,
};

describe("deck export", () => {
  it("posts the format and returns the new artifact", async () => {
    const fetchMock = vi.fn(async (url: URL, init?: RequestInit) => {
      expect(url.pathname).toBe("/v1/decks/a1/export");
      expect(init?.method).toBe("POST");
      return json(row);
    });
    vi.stubGlobal("fetch", fetchMock);
    const created = await call(decksRouter.decks.export, { artifactId: "a1", format: "pptx" });
    expect(created).toMatchObject({ id: "a2", name: "q3.pptx" });
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({ format: "pptx" });
  });

  it("says the host is starting when the engine has no runner", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ error: { code: "runner_unavailable", message: "starting" } }, 503)),
    );
    await expect(
      call(decksRouter.decks.export, { artifactId: "a1", format: "pdf" }),
    ).rejects.toMatchObject({ code: "SERVICE_UNAVAILABLE" });
  });

  it("maps a refusal to BAD_REQUEST and a missing deck to NOT_FOUND", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json({ error: { code: "invalid_input", message: "That file isn't a deck" } }, 400),
      ),
    );
    await expect(
      call(decksRouter.decks.export, { artifactId: "a1", format: "pdf" }),
    ).rejects.toMatchObject({ code: "BAD_REQUEST", message: "That file isn't a deck" });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ error: { code: "not_found", message: "Artifact not found" } }, 404)),
    );
    await expect(
      call(decksRouter.decks.export, { artifactId: "a1", format: "pdf" }),
    ).rejects.toMatchObject({ code: "NOT_FOUND" });
  });
});

describe("deck themes", () => {
  it("lists the dictionary in the web's shape", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: URL) => {
        expect(url.pathname).toBe("/v1/decks/themes");
        return json({
          default: "corporate-clean",
          themes: [
            {
              id: "corporate-clean",
              name: "Corporate Clean",
              tagline: "Sober, board-ready",
              mood: "White and navy.",
              category: "professional",
              mode: "light",
              best_for: "board reports",
              preview: "data:image/webp;base64,AA==",
            },
          ],
        });
      }),
    );
    expect(await call(decksRouter.decks.themes, {})).toEqual({
      defaultTheme: "corporate-clean",
      themes: [
        {
          id: "corporate-clean",
          name: "Corporate Clean",
          tagline: "Sober, board-ready",
          mood: "White and navy.",
          category: "professional",
          mode: "light",
          bestFor: "board reports",
          preview: "data:image/webp;base64,AA==",
        },
      ],
    });
  });

  it("returns a theme's sample deck html and maps an unknown theme to NOT_FOUND", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: URL) => {
        expect(url.pathname).toBe("/v1/decks/themes/nord/sample");
        return json({ html: "<!doctype html><title>Quarterly review</title>" });
      }),
    );
    expect(await call(decksRouter.decks.sample, { themeId: "nord" })).toEqual({
      html: "<!doctype html><title>Quarterly review</title>",
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ error: { code: "not_found", message: "Unknown theme" } }, 404)),
    );
    await expect(call(decksRouter.decks.sample, { themeId: "nope" })).rejects.toMatchObject({
      code: "NOT_FOUND",
    });
  });

  it("reads the theme id, or null for a hand-made deck", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: URL) => {
        expect(url.pathname).toBe("/v1/decks/a1/theme");
        return json({ theme: "nord" });
      }),
    );
    expect(await call(decksRouter.decks.theme, { artifactId: "a1" })).toEqual({ theme: "nord" });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ theme: null })),
    );
    expect(await call(decksRouter.decks.theme, { artifactId: "a1" })).toEqual({ theme: null });
  });
});
