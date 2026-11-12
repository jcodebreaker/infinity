import { afterEach, describe, expect, it, vi } from "vitest";
import { signalOmnigentArtifactChatOpen } from "./artifacts.js";
import {
  adoptOmnigentMuse,
  answerOmnigentAsk,
  forgetOmnigentMemoryClaim,
  getOmnigentHost,
  getOmnigentMemoryProfile,
  getOmnigentMuse,
  getOmnigentProactivity,
  getOmnigentSession,
  getOmnigentTranscript,
  listOmnigentDailyNotes,
  listOmnigentMemoryClaims,
  OmnigentApiError,
  omnigentErrorCopy,
  patchOmnigentMemoryClaim,
  postOmnigentMessage,
  putOmnigentDailyNote,
  putOmnigentMuseAgent,
  putOmnigentProactivity,
  reindexOmnigentKnowledge,
  setOmnigentHelpersPaused,
  setOmnigentMessageArtifact,
  streamOmnigentFamily,
  streamOmnigentSession,
  writeOmnigentFile,
} from "./client.js";

const CONFIG = { baseUrl: "http://omnigent.test", proxySecret: "proxy-secret", tenant: "space-1" };

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function sseResponse(frames: string[]): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame));
      controller.close();
    },
  });
  return new Response(stream, { status: 200, headers: { "content-type": "text/event-stream" } });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("omnigent client", () => {
  it("sends identity, proxy secret and the tenant on every call", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ session_id: "conv_1", agent: "superchat", created: true }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const muse = await getOmnigentMuse(CONFIG, "person@example.test");

    expect(muse).toEqual({ session_id: "conv_1", agent: "superchat", created: true });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [URL, RequestInit];
    expect(url.pathname).toBe("/v1/me/muse");
    const headers = init.headers as Record<string, string>;
    expect(headers["X-Forwarded-Email"]).toBe("person@example.test");
    expect(headers["X-Omnigent-Proxy-Secret"]).toBe("proxy-secret");
    expect(headers["X-Omnigent-Tenant"]).toBe("space-1");
  });

  it("switches and adopts the Muse through /v1/me/muse", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ session_id: "conv_1", agent: "superchat", created: false }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await putOmnigentMuseAgent(CONFIG, "e@x.test", "superchat");
    await adoptOmnigentMuse(CONFIG, "e@x.test", "conv_1");

    const [putUrl, putInit] = fetchMock.mock.calls[0] as unknown as [URL, RequestInit];
    expect(putUrl.pathname).toBe("/v1/me/muse");
    expect(putInit.method).toBe("PUT");
    expect(JSON.parse(putInit.body as string)).toEqual({ agent: "superchat" });
    const [adoptUrl, adoptInit] = fetchMock.mock.calls[1] as unknown as [URL, RequestInit];
    expect(adoptUrl.pathname).toBe("/v1/me/muse/adopt");
    expect(JSON.parse(adoptInit.body as string)).toEqual({ session_id: "conv_1" });
  });

  it("carries the engine's error code, which has short copy where it reaches the UI", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ error: { code: "muse_already_set", message: "x" } }, 409)),
    );
    const error = await adoptOmnigentMuse(CONFIG, "e@x.test", "conv_1").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(OmnigentApiError);
    expect((error as OmnigentApiError).code).toBe("muse_already_set");
    expect(omnigentErrorCopy(error)).toBe("You already have a Conversation here.");
    expect(omnigentErrorCopy(new OmnigentApiError("x", "helper_read_only"))).toBe(
      "Helpers are read-only.",
    );
    expect(omnigentErrorCopy(new OmnigentApiError("x", "not_found"))).toBeUndefined();
  });

  it("answers an ask with its choice id and note", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    await answerOmnigentAsk(CONFIG, "e@x.test", "task:obj_1:task_1", {
      choice: "answer",
      note: "Use the Q3 numbers",
    });

    const [url, init] = fetchMock.mock.calls[0] as unknown as [URL, RequestInit];
    expect(url.pathname).toBe("/v1/me/asks/task%3Aobj_1%3Atask_1/answer");
    expect(JSON.parse(init.body as string)).toEqual({
      choice: "answer",
      note: "Use the Q3 numbers",
    });
  });

  it("pauses and resumes a Conversation's helpers", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ paused: ["h1", "h2"] }))
      .mockResolvedValueOnce(jsonResponse({ resumed: ["h1"] }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(setOmnigentHelpersPaused(CONFIG, "e@x.test", "conv_1", true)).resolves.toEqual([
      "h1",
      "h2",
    ]);
    await expect(
      setOmnigentHelpersPaused(CONFIG, "e@x.test", "conv_1", false, ["h1"]),
    ).resolves.toEqual(["h1"]);

    const [pauseUrl, pauseInit] = fetchMock.mock.calls[0] as unknown as [URL, RequestInit];
    expect(pauseUrl.pathname).toBe("/v1/sessions/conv_1/helpers/pause");
    expect(JSON.parse(pauseInit.body as string)).toEqual({});
    const [resumeUrl, resumeInit] = fetchMock.mock.calls[1] as unknown as [URL, RequestInit];
    expect(resumeUrl.pathname).toBe("/v1/sessions/conv_1/helpers/resume");
    expect(JSON.parse(resumeInit.body as string)).toEqual({ helper_ids: ["h1"] });
  });

  it("reads and writes proactivity on /v1/me/proactivity", async () => {
    const prefs = { proactivity: "high", quiet_start: null, quiet_end: null, timezone: "UTC" };
    const fetchMock = vi.fn(async () => jsonResponse(prefs));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getOmnigentProactivity(CONFIG, "e@x.test")).resolves.toEqual(prefs);
    await putOmnigentProactivity(CONFIG, "e@x.test", { proactivity: "high" });

    const [, init] = fetchMock.mock.calls[1] as unknown as [URL, RequestInit];
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body as string)).toEqual({ proactivity: "high" });
  });

  it("getOmnigentSession requests a cheap snapshot and returns host_id", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ id: "conv_1", status: "running", host_id: null }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const snapshot = await getOmnigentSession(CONFIG, "e@x.test", "conv_1");

    expect(snapshot.host_id).toBeNull();
    const [url] = fetchMock.mock.calls[0] as unknown as [URL];
    expect(url.pathname).toBe("/v1/sessions/conv_1");
    expect(url.searchParams.get("include_items")).toBe("false");
    expect(url.searchParams.get("include_liveness")).toBe("false");
    expect(url.searchParams.get("include_usage")).toBe("false");
  });

  it("postOmnigentMessage throws with a descriptive error on a non-2xx response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ error: "nope" }, 400)),
    );
    await expect(postOmnigentMessage(CONFIG, "e@x.test", "conv_1", "hi")).rejects.toThrow(
      /post message event failed \(400\)/,
    );
  });

  it("postOmnigentMessage answers with the stored message's id and the turn it joined", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ queued: true, item_id: "msg_9", turn: 4 })),
    );
    expect(await postOmnigentMessage(CONFIG, "e@x.test", "conv_1", "hi")).toEqual({
      itemId: "msg_9",
      turn: 4,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 202 })),
    );
    expect(await postOmnigentMessage(CONFIG, "e@x.test", "conv_1", "hi")).toEqual({
      itemId: undefined,
      turn: undefined,
    });
  });

  it("postOmnigentMessage sends the open artifact beside the message data, never inside it", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({}));
    vi.stubGlobal("fetch", fetchMock);
    await postOmnigentMessage(CONFIG, "e@x.test", "conv_1", "bigger title", {
      openArtifact: { artifactId: "art_1", version: 3 },
    });
    const init = (fetchMock.mock.calls[0] as unknown as [URL, RequestInit])[1];
    expect(JSON.parse(String(init.body))).toEqual({
      type: "message",
      data: { role: "user", content: [{ type: "input_text", text: "bigger title" }] },
      context: { open_artifact: { artifact_id: "art_1", version: 3 } },
    });
  });

  it("postOmnigentMessage sends an ask card's pick beside the message data", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({}));
    vi.stubGlobal("fetch", fetchMock);
    await postOmnigentMessage(CONFIG, "e@x.test", "conv_1", "Nord", {
      cardAnswer: { cardId: "m1:0", optionId: "nord", label: "Nord" },
    });
    const init = (fetchMock.mock.calls[0] as unknown as [URL, RequestInit])[1];
    expect(JSON.parse(String(init.body)).context).toEqual({
      card_answer: { card_id: "m1:0", option_id: "nord", label: "Nord" },
    });
  });

  it("setOmnigentMessageArtifact posts the pick and answers with the engine's message", async () => {
    const message = {
      id: "item 1",
      role: "user",
      created_at: 100,
      blocks: [{ type: "text", text: "hi" }],
      artifact: { artifact_id: "art_2", name: "memo.md", version: null, how: "chosen" },
    };
    const fetchMock = vi.fn(async () => jsonResponse(message));
    vi.stubGlobal("fetch", fetchMock);
    expect(
      await setOmnigentMessageArtifact(CONFIG, "e@x.test", "conv_1", "item 1", "art_2"),
    ).toEqual(message);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [URL, RequestInit];
    expect(url.pathname).toBe("/v1/sessions/conv_1/items/item%201/artifact");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ artifact_id: "art_2" });

    await setOmnigentMessageArtifact(CONFIG, "e@x.test", "conv_1", "item 1", null);
    const cleared = (fetchMock.mock.calls[1] as unknown as [URL, RequestInit])[1];
    expect(JSON.parse(String(cleared.body))).toEqual({ artifact_id: null });

    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ detail: "Not Found" }, 404)),
    );
    await expect(
      setOmnigentMessageArtifact(CONFIG, "e@x.test", "conv_1", "item 1", "art_2"),
    ).rejects.toThrow(/set message artifact failed \(404\)/);
  });

  it("writeOmnigentFile PUTs base64 into the workspace; knowledge relays are session-scoped", async () => {
    const calls: Array<[string, string | undefined, unknown]> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: URL, init?: RequestInit) => {
        calls.push([url.pathname, init?.method, init?.body ? JSON.parse(String(init.body)) : null]);
        return jsonResponse({ bytes_written: 3, path: "your_files/uploads/d/a b.png", queued: 1 });
      }),
    );
    const written = await writeOmnigentFile(
      CONFIG,
      "e@x.test",
      "sess 1",
      "your_files/uploads/d/a b.png",
      new Uint8Array([1, 2, 3]),
    );
    expect(written).toEqual({ path: "your_files/uploads/d/a b.png", bytesWritten: 3 });
    expect(calls[0]).toEqual([
      "/v1/sessions/sess%201/resources/environments/default/filesystem/your_files/uploads/d/a%20b.png",
      "PUT",
      { content: "AQID", encoding: "base64", create_parents: true },
    ]);
    await expect(
      writeOmnigentFile(CONFIG, "e@x.test", "s", "../x", new Uint8Array([1])),
    ).rejects.toThrow(/outside the workspace/);
    await reindexOmnigentKnowledge(CONFIG, "e@x.test", "sess-1");
    expect(calls.at(-1)?.[0]).toBe("/v1/sessions/sess-1/knowledge/reindex");
  });

  it("streamOmnigentSession yields parsed data frames and skips [DONE]/keepalives", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        sseResponse([
          'event: session.status\ndata: {"type":"session.status","data":{"status":"running"}}\n\n',
          ": keepalive\n\n",
          'event: response.completed\ndata: {"type":"response.completed","response":{"output":[]}}\n\n',
          "data: [DONE]\n\n",
        ]),
      ),
    );

    const events = [];
    for await (const event of streamOmnigentSession(CONFIG, "e@x.test", "conv_1")) {
      events.push(event);
    }
    expect(events).toEqual([
      { type: "session.status", data: { status: "running" } },
      { type: "response.completed", response: { output: [] } },
    ]);
  });
});

describe("getOmnigentMemoryProfile", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("reads the Super Chat's profile and maps an empty one to null", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ profile: "" }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await getOmnigentMemoryProfile(CONFIG, "person@example.test", "conv_1");

    expect(result).toEqual({ profile: null });
    const [url] = fetchMock.mock.calls[0] as unknown as [URL];
    expect(String(url)).toBe("http://omnigent.test/v1/sessions/conv_1/memory/profile");
  });
});

describe("daily notes", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("lists the owner's notes and saves one with PUT", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ daily_notes: [], date: "2026-10-04" }));
    vi.stubGlobal("fetch", fetchMock);

    expect(await listOmnigentDailyNotes(CONFIG, "p@example.test", 7)).toEqual([]);
    await putOmnigentDailyNote(CONFIG, "p@example.test", "2026-10-04", { decisions: "- x" });

    const [listUrl] = fetchMock.mock.calls[0] as unknown as [URL];
    expect(String(listUrl)).toBe("http://omnigent.test/v1/me/daily-notes?limit=7");
    const [putUrl, init] = fetchMock.mock.calls[1] as unknown as [URL, RequestInit];
    expect(String(putUrl)).toBe("http://omnigent.test/v1/me/daily-notes/2026-10-04");
    expect(init.method).toBe("PUT");
    expect(init.body).toBe(JSON.stringify({ sections: { decisions: "- x" } }));
  });
});

describe("throwOnError redaction (an earlier wiring review)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("redacts a secret value out of the error body before it reaches the thrown message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response("request failed: Bearer sk-live-topsecret was rejected", { status: 500 }),
      ),
    );
    const config = { ...CONFIG, secrets: ["sk-live-topsecret"] };

    await expect(getOmnigentSession(config, "p@x.test", "conv_1")).rejects.toThrow(/\[redacted\]/);
    await expect(getOmnigentSession(config, "p@x.test", "conv_1")).rejects.not.toThrow(
      /sk-live-topsecret/,
    );
  });

  it("drops the body entirely when no secrets list is configured, rather than risking a leak", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("request failed: Bearer sk-live-topsecret", { status: 500 })),
    );

    await expect(getOmnigentSession(CONFIG, "p@x.test", "conv_1")).rejects.not.toThrow(
      /sk-live-topsecret/,
    );
  });
});

describe("host status", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("host status hits the session-scoped route with identity/proxy headers", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({ ready: true, launch: null }));
    vi.stubGlobal("fetch", fetchMock);

    expect(await getOmnigentHost(CONFIG, "p@example.test", "s 1")).toEqual({
      ready: true,
      launch: null,
    });

    const calls = fetchMock.mock.calls as unknown as [URL, RequestInit][];
    expect(calls.map(([url, init]) => `${init?.method ?? "GET"} ${url.pathname}`)).toEqual([
      "GET /v1/sessions/s%201/host",
    ]);
    expect((calls[0]![1].headers as Record<string, string>)["X-Omnigent-Proxy-Secret"]).toBe(
      "proxy-secret",
    );
  });
});

describe("memory claims", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("lists, edits with PATCH and forgets with confirm", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ claims: [] }));
    vi.stubGlobal("fetch", fetchMock);

    expect(await listOmnigentMemoryClaims(CONFIG, "p@example.test", "s1")).toEqual([]);
    await patchOmnigentMemoryClaim(CONFIG, "p@example.test", "s1", "c1", "Maya, manager");
    await forgetOmnigentMemoryClaim(CONFIG, "p@example.test", "s1", "c1");

    const [listUrl] = fetchMock.mock.calls[0] as unknown as [URL];
    expect(String(listUrl)).toBe("http://omnigent.test/v1/sessions/s1/memory/claims");
    const [patchUrl, patch] = fetchMock.mock.calls[1] as unknown as [URL, RequestInit];
    expect(String(patchUrl)).toBe("http://omnigent.test/v1/sessions/s1/memory/claims/c1");
    expect(patch.method).toBe("PATCH");
    expect(patch.body).toBe(JSON.stringify({ text: "Maya, manager" }));
    const [, forget] = fetchMock.mock.calls[2] as unknown as [URL, RequestInit];
    expect(forget.body).toBe(JSON.stringify({ claim_id: "c1", confirm: true }));
  });
});

describe("transcript and family stream", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("reads one transcript page with its cursor", async () => {
    const page = {
      data: [],
      has_more: false,
      older_cursor: null,
      lineage: { kind: "super", root_id: "s1", parent_id: null },
    };
    const fetchMock = vi.fn(async () => jsonResponse(page));
    vi.stubGlobal("fetch", fetchMock);

    expect(
      await getOmnigentTranscript(CONFIG, "p@example.test", "s 1", {
        limit: 20,
        before: "i9",
      }),
    ).toEqual(page);

    const [url] = fetchMock.mock.calls[0] as unknown as [URL];
    expect(String(url)).toBe(
      "http://omnigent.test/v1/sessions/s%201/transcript?limit=20&before=i9",
    );
  });

  it("yields the family events and ignores any other frame", async () => {
    const fetchMock = vi.fn(async () =>
      sseResponse([
        'event: message.done\ndata: {"type":"message.done","chat_id":"c1","item_id":"i1"}\n\n',
        'event: message.delivery\ndata: {"type":"message.delivery","chat_id":"c1","item_id":"i2","delivered":"steer"}\n\n',
        'event: other\ndata: {"type":"other"}\n\n',
        'event: round.updated\ndata: {"type":"round.updated","root_id":"s1","round_id":"r1","status":"done","started_at":1,"live":null,"helpers":0,"files":1,"steps":3}\n\n',
        'event: session.heartbeat\ndata: {"type":"session.heartbeat"}\n\n',
      ]),
    );
    vi.stubGlobal("fetch", fetchMock);

    const events = [];
    for await (const event of streamOmnigentFamily(CONFIG, "p@example.test", "s1")) {
      events.push(event);
    }

    expect(events).toEqual([
      { type: "message.done", chat_id: "c1", item_id: "i1" },
      { type: "message.delivery", chat_id: "c1", item_id: "i2", delivered: "steer" },
      {
        type: "round.updated",
        root_id: "s1",
        round_id: "r1",
        status: "done",
        started_at: 1,
        live: null,
        helpers: 0,
        files: 1,
        steps: 3,
      },
      { type: "session.heartbeat" },
    ]);
    const [url] = fetchMock.mock.calls[0] as unknown as [URL];
    expect(String(url)).toBe("http://omnigent.test/v1/sessions/s1/family/stream");
  });
});

describe("a file's studio is open", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("posts to the file's chat/open route and returns the engine's answer", async () => {
    const calls: Array<[string, string | undefined]> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: URL | string, init?: RequestInit) => {
        calls.push([new URL(String(url)).pathname, init?.method]);
        return jsonResponse({ session_id: "studio-1", warm: true });
      }),
    );
    await expect(signalOmnigentArtifactChatOpen(CONFIG, "e@x.test", "art 1")).resolves.toEqual({
      session_id: "studio-1",
      warm: true,
    });
    expect(calls).toEqual([["/v1/artifacts/art%201/chat/open", "POST"]]);
  });
});
