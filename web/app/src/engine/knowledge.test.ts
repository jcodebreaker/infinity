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

const { knowledgeRouter } = await import("./knowledge");

const FILE = "f".repeat(32);

describe("knowledge", () => {
  it("maps the Muse session's file states to the camelCase shape", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: URL) => {
        expect(url.pathname).toBe("/v1/sessions/sess-1/knowledge/status");
        return json({
          files: [
            {
              file_id: FILE,
              path: "your_files/uploads/2026-10-09/report.pdf",
              name: "report.pdf",
              kind: "pdf",
              state: "searchable",
              search: "keyword",
              keyword_only_reason: "no_connection",
              pages: 12,
              pages_without_text: 2,
              error: null,
              artifact_id: null,
            },
          ],
          embeddings: { available: false, reason: "no_connection" },
          host: "asleep",
          updated_at: 1760000000,
        });
      }),
    );
    expect(await call(knowledgeRouter.knowledge.status, {})).toEqual({
      files: [
        {
          fileId: FILE,
          path: "your_files/uploads/2026-10-09/report.pdf",
          name: "report.pdf",
          kind: "pdf",
          state: "searchable",
          search: "keyword",
          keywordOnlyReason: "no_connection",
          pages: 12,
          pagesWithoutText: 2,
          error: null,
          artifactId: null,
        },
      ],
      embeddings: { available: false, reason: "no_connection" },
      host: "asleep",
      updatedAt: 1760000000,
    });
  });

  it("searches the Muse session and maps the passages", async () => {
    const fetchMock = vi.fn(async () =>
      json({
        mode: "hybrid",
        keyword_only_reason: null,
        files_indexing: 1,
        files_indexing_names: ["new.pdf"],
        results: [
          {
            file_id: FILE,
            path: "your_files/uploads/d/report.pdf",
            file_name: "report.pdf",
            artifact_id: "a".repeat(32),
            page: 3,
            page_end: 3,
            score: 0.8,
            heading: "Risks",
            passage: "Currency exposure",
            thumbnail_url: "/x",
          },
          {
            file_id: "e".repeat(32),
            path: "your_files/notes.md",
            file_name: "notes.md",
            artifact_id: null,
            page: 1,
            page_end: 1,
            score: 0.4,
            heading: "",
            passage: "text",
            thumbnail_url: null,
          },
        ],
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const found = (await call(knowledgeRouter.knowledge.search, {
      query: "currency",
      fileIds: [FILE],
      k: 4,
    })) as {
      filesIndexing: number;
      filesIndexingNames: string[];
      results: {
        fileName: string;
        fileId: string;
        artifactId: string | null;
        hasThumbnail: boolean;
      }[];
    };
    const [url, init] = fetchMock.mock.calls[0] as unknown as [URL, RequestInit];
    expect(url.pathname).toBe("/v1/sessions/sess-1/knowledge/search");
    expect(JSON.parse(String(init.body))).toEqual({ query: "currency", file_ids: [FILE], k: 4 });
    expect(found.filesIndexing).toBe(1);
    expect(found.filesIndexingNames).toEqual(["new.pdf"]);
    expect(
      found.results.map((hit) => [hit.fileName, hit.fileId, hit.artifactId, hit.hasThumbnail]),
    ).toEqual([
      ["report.pdf", FILE, "a".repeat(32), true],
      ["notes.md", "e".repeat(32), null, false],
    ]);
  });

  it("returns a page thumbnail by file id and maps a missing page to not found", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: URL) => {
        calls.push(url.pathname);
        if (url.pathname.endsWith("/pages/9/thumbnail")) {
          return new Response("nope", { status: 404 });
        }
        return new Response(new Uint8Array([82, 73, 70, 70]), { status: 200 });
      }),
    );
    expect(await call(knowledgeRouter.knowledge.pageThumbnail, { fileId: FILE, page: 2 })).toEqual({
      contentBase64: "UklGRg==",
      mimeType: "image/webp",
    });
    expect(calls[0]).toBe(`/v1/sessions/sess-1/knowledge/files/${FILE}/pages/2/thumbnail`);
    await expect(
      call(knowledgeRouter.knowledge.pageThumbnail, { fileId: FILE, page: 9 }),
    ).rejects.toMatchObject({ code: "NOT_FOUND" });
  });

  it("asks the Muse session to re-embed", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: URL, init?: RequestInit) => {
        expect(url.pathname).toBe("/v1/sessions/sess-1/knowledge/reindex");
        expect(init?.method).toBe("POST");
        return json({ queued: 3 });
      }),
    );
    expect(await call(knowledgeRouter.knowledge.reindex, {})).toEqual({ queued: 3 });
  });
});
