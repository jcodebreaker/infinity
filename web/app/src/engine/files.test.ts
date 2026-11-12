import { createHash } from "node:crypto";
import { call } from "@orpc/server";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./client", () => ({
  engineClient: () => ({ baseUrl: "http://engine.test", proxySecret: "proxy", secrets: [] }),
  museSessionId: async () => "muse-1",
  NO_EMAIL: "",
}));

const { dedupeUploadName, filesRouter, ingestAttachment, safeUploadName, uploadAttachment } =
  await import("./files");

const botId = "bot-1";
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const fileBody = (content: string, extra: Record<string, unknown> = {}) => ({
  object: "session.environment.filesystem.file_content",
  path: "a/b.html",
  encoding: "utf-8",
  content,
  bytes: content.length,
  ...extra,
});

afterEach(() => vi.unstubAllGlobals());

describe("engine files", () => {
  it("lists a directory newest first with the engine's entry shape mapped", async () => {
    const fetchMock = vi.fn(async (url: URL) => {
      expect(url.pathname).toBe("/v1/sessions/muse-1/resources/environments/default/filesystem/a");
      return json({
        data: [
          { name: "old.txt", path: "a/old.txt", type: "file", bytes: 3, modified_at: 10 },
          { name: "sub", path: "a/sub", type: "directory", bytes: null, modified_at: 30 },
          { name: "new.md", path: "a/new.md", type: "file", bytes: 5, modified_at: 20 },
        ],
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const { entries } = await call(filesRouter.files.list, { botId, path: "a/" });
    expect(entries.map((e) => e.name)).toEqual(["sub", "new.md", "old.txt"]);
    expect(entries[0]).toMatchObject({ type: "directory", size: null });
  });

  it("maps an engine 503 (runner still reconnecting) to SERVICE_UNAVAILABLE", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ error: { code: "runner_unavailable", message: "offline" } }, 503)),
    );
    await expect(call(filesRouter.files.list, { botId, path: "" })).rejects.toMatchObject({
      code: "SERVICE_UNAVAILABLE",
    });
  });

  it("rejects paths that leave the workspace without calling the engine", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    for (const path of ["../etc/passwd", "a/../../x", "/etc/passwd", "~/x", "a\\b"]) {
      await expect(call(filesRouter.files.read, { botId, path })).rejects.toThrow(
        /outside the workspace/,
      );
    }
    await expect(call(filesRouter.files.list, { botId, path: ".." })).rejects.toThrow(
      /outside the workspace/,
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("reads text with its mime and withholds oversized or opaque binary content", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json(fileBody("<h1>hi</h1>"))),
    );
    const html = await call(filesRouter.files.read, { botId, path: "a/b.html" });
    expect(html).toMatchObject({ name: "b.html", mimeType: "text/html", tooLarge: false });
    expect(Buffer.from(html.contentBase64 ?? "", "base64").toString()).toBe("<h1>hi</h1>");

    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json(fileBody("x", { bytes: 6 * 1024 * 1024 }))),
    );
    const big = await call(filesRouter.files.read, { botId, path: "a/b.txt" });
    expect(big).toMatchObject({ tooLarge: true, contentBase64: null });

    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json({
          ...fileBody(""),
          encoding: "base64",
          content: Buffer.from([0, 1, 2, 3]).toString("base64"),
        }),
      ),
    );
    const bin = await call(filesRouter.files.read, { botId, path: "a/blob.bin" });
    expect(bin).toMatchObject({ binary: true, contentBase64: null });
  });

  it("saves a workspace file to the Library through POST /v1/artifacts", async () => {
    const fetchMock = vi.fn(async (url: URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        expect(url.pathname).toBe("/v1/artifacts");
        expect(url.searchParams.get("parent_session_id")).toBe("muse-1");
        expect(url.searchParams.get("name")).toBe("b.html");
        expect((init.body as Uint8Array).length).toBe(11);
        return json(
          {
            id: "a1",
            parent_session_id: "muse-1",
            name: "b.html",
            title: null,
            kind: "html",
            mime: "text/html; charset=utf-8",
            size: 11,
            version: 1,
            versions: 1,
            published: false,
            created_at: 1_700_000_000,
            updated_at: null,
          },
          201,
        );
      }
      return json(fileBody("<h1>hi</h1>"));
    });
    vi.stubGlobal("fetch", fetchMock);
    const artifact = await call(filesRouter.files.saveToLibrary, { botId, path: "a/b.html" });
    expect(artifact).toMatchObject({ id: "a1", botId: "muse-1", mimeType: "text/html", size: 11 });
  });
});

describe("engine attachment upload", () => {
  const FS = "/v1/sessions/muse-1/resources/environments/default/filesystem";
  const day = new Date("2026-10-09T12:00:00Z");
  const input = (over: Record<string, unknown> = {}) => ({
    name: "report.pdf",
    mimeType: "application/pdf",
    contentBase64: Buffer.from("%PDF-1.4").toString("base64"),
    ...over,
  });
  const museOr = (
    handler: (url: URL, init?: RequestInit) => Response | Promise<Response>,
  ): ReturnType<typeof vi.fn> => {
    const fetchMock = vi.fn(async (url: URL, init?: RequestInit) =>
      url.pathname === "/v1/me/muse"
        ? json({ session_id: "muse-1", agent: "agent", created: false })
        : handler(url, init),
    );
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  };

  it("PUTs base64 under your_files/uploads/<date>/ and returns the path", async () => {
    const fetchMock = museOr((url, init) => {
      if (url.pathname.endsWith("/knowledge/find")) return json({ found: false });
      if (init?.method === "PUT") return json({ bytes_written: 8, path: "x", created: true });
      expect(url.pathname).toMatch(/\/your_files\/uploads(\/2026-10-09)?$/);
      return json({ error: { code: "not_found" } }, 404);
    });
    const out = await uploadAttachment(input(), day);
    expect(out).toEqual({
      path: "your_files/uploads/2026-10-09/report.pdf",
      name: "report.pdf",
      mimeType: "application/pdf",
      size: 8,
    });
    const [url, init] = fetchMock.mock.calls.at(-1) as unknown as [URL, RequestInit];
    expect(url.pathname).toBe(`${FS}/your_files/uploads/2026-10-09/report.pdf`);
    expect(init.method).toBe("PUT");
    expect(JSON.parse(String(init.body))).toEqual({
      content: Buffer.from("%PDF-1.4").toString("base64"),
      encoding: "base64",
      create_parents: true,
      if_exists: "fail",
    });
  });

  it("de-duplicates names against the day's folder", async () => {
    museOr((url, init) =>
      url.pathname.endsWith("/knowledge/find")
        ? json({ found: false })
        : init?.method === "PUT"
          ? json({ bytes_written: 8 })
          : json({
              data: ["report.pdf", "report (2).pdf"].map((name) => ({
                name,
                path: `your_files/uploads/2026-10-09/${name}`,
                type: "file",
                bytes: 1,
                modified_at: 1,
              })),
            }),
    );
    const out = await uploadAttachment(input(), day);
    expect(out.path).toBe("your_files/uploads/2026-10-09/report (3).pdf");
    expect(out.name).toBe("report (3).pdf");
  });

  describe("re-attaching the same file", () => {
    const DAY = "your_files/uploads/2026-10-09";
    const sha = createHash("sha256").update("%PDF-1.4").digest("hex");

    function world(found: Record<string, unknown>) {
      const puts: string[] = [];
      const finds: unknown[] = [];
      museOr((url, init) => {
        if (url.pathname.endsWith("/knowledge/find")) {
          finds.push(JSON.parse(String(init?.body)));
          return json(found);
        }
        if (init?.method === "PUT") {
          puts.push(decodeURIComponent(url.pathname));
          return json({ bytes_written: 8 });
        }
        return json({ data: [] });
      });
      return { puts, finds };
    }

    it("asks the host once, by hash, and reuses the stored copy: nothing is written", async () => {
      const { puts, finds } = world({
        found: true,
        file_id: "ab",
        name: "report.pdf",
        path: `${DAY}/report.pdf`,
        pages: 1,
      });
      const out = await uploadAttachment(input(), day);
      expect(out).toMatchObject({ path: `${DAY}/report.pdf`, name: "report.pdf", size: 8 });
      expect(finds).toEqual([{ sha256: sha }]);
      expect(puts).toEqual([]);
    });

    it("finds a copy from any earlier day: the answer does not depend on folders", async () => {
      const { puts } = world({
        found: true,
        name: "report (2).pdf",
        path: "your_files/uploads/2026-08-01/report (2).pdf",
      });
      const out = await uploadAttachment(input(), day);
      expect(out.path).toBe("your_files/uploads/2026-08-01/report (2).pdf");
      expect(puts).toEqual([]);
    });

    it("stores the file when no copy has these bytes; a name clash still gets a number", async () => {
      const puts: string[] = [];
      museOr((url, init) => {
        if (url.pathname.endsWith("/knowledge/find")) return json({ found: false });
        if (init?.method === "PUT") {
          puts.push(decodeURIComponent(url.pathname));
          return json({ bytes_written: 8 });
        }
        return json({
          data: [
            {
              name: "report.pdf",
              path: `${DAY}/report.pdf`,
              type: "file",
              bytes: 3,
              modified_at: 1,
            },
          ],
        });
      });
      const out = await uploadAttachment(input(), day);
      expect(out.path).toBe(`${DAY}/report (2).pdf`);
      expect(puts).toEqual([`${FS}/${DAY}/report (2).pdf`]);
    });
  });

  describe("names, images and races", () => {
    const DAY = "your_files/uploads/2026-10-09";

    it("keeps the name the person chose: a copy under another name is not reused", async () => {
      const writes: string[] = [];
      museOr((url, init) => {
        if (url.pathname.endsWith("/knowledge/find")) {
          return json({ found: true, name: "report.pdf", path: `${DAY}/report.pdf` });
        }
        if (init?.method === "PUT") {
          writes.push(decodeURIComponent(url.pathname));
          return json({ bytes_written: 8 });
        }
        return json({ data: [] });
      });
      const out = await uploadAttachment(input({ name: "Q3-final.pdf" }), day);
      expect(out.name).toBe("Q3-final.pdf");
      expect(writes).toEqual([`${FS}/${DAY}/Q3-final.pdf`]);
    });

    it("never hashes or looks up an image: it is not indexed", async () => {
      const finds: string[] = [];
      museOr((url, init) => {
        if (url.pathname.endsWith("/knowledge/find")) finds.push(url.pathname);
        return init?.method === "PUT" ? json({ bytes_written: 3 }) : json({ data: [] });
      });
      const out = await uploadAttachment(input({ name: "pic.png", mimeType: "image/png" }), day);
      expect(out.path).toBe(`${DAY}/pic.png`);
      expect(finds).toEqual([]);
    });

    it("picks the next name when another upload took the free one first", async () => {
      const written: string[] = [];
      let listed = 0;
      museOr((url, init) => {
        if (url.pathname.endsWith("/knowledge/find")) return json({ found: false });
        if (init?.method === "PUT") {
          const path = decodeURIComponent(url.pathname);
          if (path.endsWith("/report.pdf")) {
            return json({ error: { code: "already_exists", message: "exists" } }, 409);
          }
          written.push(path);
          return json({ bytes_written: 8 });
        }
        listed += 1;
        // the folder listing is stale on the first look: the other upload is not in it yet
        return json({
          data:
            listed === 1
              ? []
              : [
                  {
                    name: "report.pdf",
                    path: `${DAY}/report.pdf`,
                    type: "file",
                    bytes: 8,
                    modified_at: 1,
                  },
                ],
        });
      });
      const out = await uploadAttachment(input(), day);
      expect(out.name).toBe("report (2).pdf");
      expect(written).toEqual([`${FS}/${DAY}/report (2).pdf`]);
    });
  });

  it("maps a 503 from the engine to the calm starting message", async () => {
    museOr(() => json({ error: { code: "runner_unavailable" } }, 503));
    await expect(uploadAttachment(input(), day)).rejects.toMatchObject({
      code: "SERVICE_UNAVAILABLE",
      message: "Your machine is starting. Try again in a moment.",
    });
  });

  it("rejects bad types and bad base64 before touching the engine", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(uploadAttachment(input({ mimeType: "application/zip" }))).rejects.toMatchObject({
      code: "BAD_REQUEST",
    });
    await expect(uploadAttachment(input({ contentBase64: "not base64!" }))).rejects.toMatchObject({
      code: "BAD_REQUEST",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("makes names safe and numbers duplicates", () => {
    expect(safeUploadName("../../etc/passwd", "text/plain")).toBe("passwd.txt");
    expect(safeUploadName("..\\x\\.hidden.png", "image/png")).toBe("hidden.png");
    expect(safeUploadName("", "application/pdf")).toBe("file.pdf");
    expect(safeUploadName("Shot 4.40.50\u202fPM.png", "image/png")).toBe("Shot 4.40.50 PM.png");
    expect(dedupeUploadName("a.pdf", new Set(["a.pdf"]))).toBe("a (2).pdf");
    expect(dedupeUploadName("noext", new Set(["noext", "noext (2)"]))).toBe("noext (3)");
  });
});

describe("engine attachment ingest", () => {
  const path = "your_files/uploads/2026-10-09/report.pdf";
  const stub = (handler: (url: URL, init?: RequestInit) => Response) => {
    const fetchMock = vi.fn(async (url: URL, init?: RequestInit) =>
      url.pathname === "/v1/me/muse"
        ? json({ session_id: "muse-1", agent: "agent", created: false })
        : handler(url, init),
    );
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  };

  it("asks the engine to read the file and returns what it came to", async () => {
    const fetchMock = stub(() =>
      json({ file_id: "ab12", name: "report.pdf", pages: 3, chars: 900, markdown_path: "/m.md" }),
    );
    const out = await ingestAttachment({ path });
    expect(out).toEqual({ fileId: "ab12", name: "report.pdf", pages: 3, chars: 900 });
    const [url, init] = fetchMock.mock.calls.at(-1) as unknown as [URL, RequestInit];
    expect(url.pathname).toBe("/v1/sessions/muse-1/knowledge/ingest");
    expect(JSON.parse(String(init.body))).toEqual({ path });
  });

  it("only reads uploads, and says calmly when the file can't be read", async () => {
    const fetchMock = stub(() =>
      json({ error: { code: "invalid_input", message: "not a readable PDF" } }, 400),
    );
    await expect(ingestAttachment({ path: "secrets/x.pdf" })).rejects.toMatchObject({
      code: "BAD_REQUEST",
    });
    expect(fetchMock).not.toHaveBeenCalled();
    await expect(ingestAttachment({ path })).rejects.toMatchObject({
      code: "BAD_REQUEST",
      message: "Superchat couldn't read this file.",
    });
  });

  it("maps a starting host to the calm message", async () => {
    stub(() => json({ error: { code: "runner_unavailable" } }, 503));
    await expect(ingestAttachment({ path })).rejects.toMatchObject({ code: "SERVICE_UNAVAILABLE" });
  });

  it("maps by the engine's error code, not by words in its message", async () => {
    // A 503 whose text mentions nothing recognisable is still "starting" because of its code ...
    stub(() => json({ error: { code: "runner_unavailable", message: "no" } }, 503));
    await expect(ingestAttachment({ path })).rejects.toMatchObject({ code: "SERVICE_UNAVAILABLE" });
    // ... and a different 503 is not (no code match, so it is not shown as "starting").
    stub(() => json({ error: { code: "internal_error", message: "(503) upstream" } }, 503));
    await expect(ingestAttachment({ path })).rejects.not.toMatchObject({
      code: "SERVICE_UNAVAILABLE",
    });
  });

  it("answers a slow read with its own message, never 'starting'", async () => {
    stub(() => json({ error: { code: "knowledge_timeout", message: "slow" } }, 504));
    await expect(ingestAttachment({ path })).rejects.toMatchObject({
      code: "GATEWAY_TIMEOUT",
      message: "Reading this file is taking too long. Try again in a moment.",
    });
  });
});
