// @vitest-environment jsdom

import { ORPCError } from "@orpc/client";
import type { Bot, HostStatus, ThreadMessage } from "@superchat/contracts";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

/** The host status the composer waits on, driven by the tests. */
const hostBus = (() => {
  const listeners = new Set<(status: HostStatus | null) => void>();
  let current: HostStatus | null = null;
  return {
    feed: {
      current: () => current,
      subscribe: (listener: (status: HostStatus | null) => void) => {
        listeners.add(listener);
        return () => {
          listeners.delete(listener);
        };
      },
    },
    emit(status: Partial<HostStatus> | null) {
      current = status as HostStatus | null;
      for (const listener of listeners) listener(current);
    },
    reset() {
      listeners.clear();
      current = null;
    },
  };
})();
const STARTING = { state: "booting", launch: { stage: "starting" } } as const;
const UP = { state: "running", runnerReady: true } as const;

const api = vi.hoisted(() => ({
  files: { uploadAttachment: vi.fn(), ingestAttachment: vi.fn() },
  chats: { send: vi.fn(), interrupt: vi.fn() },
  asks: { answer: vi.fn() },
  routines: { testRun: vi.fn() },
}));
vi.mock("../../../lib/rpc", () => ({ rpc: api }));
vi.mock("./hostReady", async (orig) => ({
  ...(await orig<typeof import("./hostReady")>()),
  pollingHostFeed: () => hostBus.feed,
}));
vi.mock("react-dom/client", async (orig) =>
  (await import("../../../test/i18n")).withI18nRoot(
    await orig<typeof import("react-dom/client")>(),
  ),
);
const notifyAsksChanged = vi.hoisted(() => vi.fn());
vi.mock("../../../features/approvals", () => ({ notifyAsksChanged }));
vi.mock("../../../lib/browser-notifications", () => ({
  requestBrowserNotificationPermission: () => null,
}));
vi.mock("../intro", () => ({ markFirstRunSeen: vi.fn() }));

import { useComposerSend } from "./useComposerSend";

const BOT = "b".repeat(32);
const roots: Root[] = [];
let latest: ReturnType<typeof useComposerSend>;

const botRef = { current: BOT as string | undefined };
const transcript = {
  beginSend: vi.fn(),
  failSend: vi.fn(),
  stopWaiting: vi.fn(),
  refresh: vi.fn(),
};
let shown: ThreadMessage[] = [];
const activeId = BOT;

function Harness() {
  latest = useComposerSend({
    active: { id: activeId } as Bot,
    activeBotId: botRef,
    userId: "u1",
    messages: shown,
    transcript,
    botsRef: { current: [] },
    flushPendingBrowserNotifications: () => undefined,
  });
  return null;
}

async function pickFiles(...files: [string, string, string][]) {
  const list = files.map(([n, b, t]) => new File([b], n, { type: t, lastModified: 1 }));
  await act(async () =>
    latest.onAttachmentPick(Object.assign(list, { item: (i: number) => list[i] ?? null }) as never),
  );
}

beforeEach(() => {
  shown = [];
  hostBus.reset();
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.stubGlobal(
    "URL",
    Object.assign(URL, { createObjectURL: () => "blob:x", revokeObjectURL() {} }),
  );
  api.files.uploadAttachment.mockReset().mockImplementation(async (input: { name: string }) => ({
    path: `your_files/uploads/2026-10-09/${input.name}`,
    name: input.name,
    mimeType: "application/pdf",
    size: 3,
  }));
  api.files.ingestAttachment.mockReset().mockImplementation(async (input: { path: string }) => ({
    fileId: "ab12",
    name: input.path.split("/").pop(),
    pages: 2,
    chars: 40,
  }));
  transcript.beginSend.mockReset();
  transcript.failSend.mockReset();
  transcript.stopWaiting.mockReset();
  transcript.refresh.mockReset();
  api.chats.interrupt.mockReset().mockResolvedValue({ delivered: true });
  api.asks.answer.mockReset().mockResolvedValue({ ok: true });
  api.chats.send.mockReset().mockResolvedValue({ ok: true });
});
afterEach(async () => {
  for (const root of roots.splice(0)) await act(async () => root.unmount());
});

async function mount() {
  const host = document.createElement("div");
  const root = createRoot(host);
  roots.push(root);
  await act(async () => root.render(<Harness />));
}

const settled = () => act(async () => new Promise((resolve) => setTimeout(resolve, 20)));
const statuses = () => latest.activePendingAttachments.map((a) => a.status);

it("uploads and reads each attachment when it is picked, then sends them as workspace attachments", async () => {
  await mount();
  await pickFiles(["a.pdf", "aaa", "application/pdf"], ["b.pdf", "bbb", "application/pdf"]);
  await settled();
  expect(statuses()).toEqual(["ready", "ready"]);
  expect(api.files.uploadAttachment).toHaveBeenCalledTimes(2);
  expect(api.files.uploadAttachment.mock.calls[0]?.[0]).toMatchObject({
    botId: BOT,
    name: "a.pdf",
    mimeType: "application/pdf",
    contentBase64: "YWFh",
  });
  expect(api.files.ingestAttachment.mock.calls.map((c) => c[0])).toEqual([
    { botId: BOT, path: "your_files/uploads/2026-10-09/a.pdf" },
    { botId: BOT, path: "your_files/uploads/2026-10-09/b.pdf" },
  ]);
  await act(async () => latest.sendMessage("read these"));
  expect(api.files.uploadAttachment).toHaveBeenCalledTimes(2); // not uploaded again at send
  const sent = api.chats.send.mock.calls[0]?.[0];
  expect(sent).toMatchObject({ botId: BOT, text: "read these" });
  expect(sent.attachments).toEqual([
    expect.objectContaining({ path: "your_files/uploads/2026-10-09/a.pdf", name: "a.pdf" }),
    expect.objectContaining({ path: "your_files/uploads/2026-10-09/b.pdf", name: "b.pdf" }),
  ]);
  expect(latest.activePendingAttachments).toHaveLength(0);
});

it("goes uploading, reading, ready, and holds the message back until then", async () => {
  await mount();
  let release: () => void = () => undefined;
  api.files.ingestAttachment.mockImplementation(
    () =>
      new Promise((resolve) => {
        release = () => resolve({ fileId: "ab12", name: "a.pdf", pages: 1, chars: 3 });
      }),
  );
  let finishUpload: () => void = () => undefined;
  api.files.uploadAttachment.mockImplementation(
    (input: { name: string }) =>
      new Promise((resolve) => {
        finishUpload = () =>
          resolve({
            path: `p/${input.name}`,
            name: input.name,
            mimeType: "application/pdf",
            size: 3,
          });
      }),
  );
  await pickFiles(["a.pdf", "aaa", "application/pdf"]);
  await settled();
  expect(statuses()).toEqual(["uploading"]);
  expect(await act(async () => latest.sendMessage("hi"))).toBe(false);
  await act(async () => finishUpload());
  await settled();
  expect(statuses()).toEqual(["reading"]);
  expect(await act(async () => latest.sendMessage("hi"))).toBe(false);
  expect(api.chats.send).not.toHaveBeenCalled();
  await act(async () => release());
  await settled();
  expect(statuses()).toEqual(["ready"]);
  expect(await act(async () => latest.sendMessage("hi"))).toBe(true);
});

it("waits for the host's own 'up' event while uploading, with no timer retry", async () => {
  vi.useFakeTimers();
  try {
    await mount();
    api.files.uploadAttachment
      .mockRejectedValueOnce(new ORPCError("SERVICE_UNAVAILABLE", { message: "starting" }))
      .mockResolvedValueOnce({
        path: "p/a.pdf",
        name: "a.pdf",
        mimeType: "application/pdf",
        size: 3,
      });
    await pickFiles(["a.pdf", "aaa", "application/pdf"]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(statuses()).toEqual(["starting"]);
    expect(await act(async () => latest.sendMessage("hi"))).toBe(false); // Send stays held
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000); // a minute passes: nothing retries by itself
    });
    expect(api.files.uploadAttachment).toHaveBeenCalledTimes(1);
    expect(statuses()).toEqual(["starting"]);
    await act(async () => hostBus.emit(STARTING));
    expect(api.files.uploadAttachment).toHaveBeenCalledTimes(1);
    await act(async () => hostBus.emit(UP));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(api.files.uploadAttachment).toHaveBeenCalledTimes(2);
    expect(statuses()).toEqual(["ready"]);
  } finally {
    vi.useRealTimers();
  }
});

it("shows a calm error on the chip when the host never comes up, and retries", async () => {
  vi.useFakeTimers();
  try {
    await mount();
    api.files.uploadAttachment.mockRejectedValue(
      new ORPCError("SERVICE_UNAVAILABLE", { message: "starting" }),
    );
    await pickFiles(["a.pdf", "aaa", "application/pdf"]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(125_000);
    });
    expect(statuses()).toEqual(["error"]);
    expect(latest.activePendingAttachments[0]?.error).toBe(
      "Your machine is starting. Try again in a moment.",
    );
    expect(api.files.uploadAttachment).toHaveBeenCalledTimes(1);
    expect(await act(async () => latest.sendMessage("hi"))).toBe(false);
    api.files.uploadAttachment.mockReset().mockResolvedValue({
      path: "p/a.pdf",
      name: "a.pdf",
      mimeType: "application/pdf",
      size: 3,
    });
    const failed = latest.activePendingAttachments[0] as never;
    await act(async () => latest.retryAttachment(failed));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(statuses()).toEqual(["ready"]);
  } finally {
    vi.useRealTimers();
  }
});

it("keeps the draft and the file when reading fails, and lets the person retry or remove it", async () => {
  await mount();
  api.files.ingestAttachment.mockRejectedValueOnce(new Error("boom"));
  await pickFiles(["a.pdf", "aaa", "application/pdf"]);
  await settled();
  expect(statuses()).toEqual(["error"]);
  expect(latest.activePendingAttachments[0]?.error).toBeTruthy();
  await act(async () => latest.retryAttachment(latest.activePendingAttachments[0] as never));
  await settled();
  expect(statuses()).toEqual(["ready"]);
  expect(api.files.uploadAttachment).toHaveBeenCalledTimes(1); // the retry only reads again
  await act(async () => latest.removeAttachment(latest.activePendingAttachments[0] as never));
  expect(latest.activePendingAttachments).toHaveLength(0);
});

it("sends an image without reading it", async () => {
  await mount();
  await pickFiles(["p.png", "png", "image/png"]);
  await settled();
  expect(statuses()).toEqual(["ready"]);
  expect(api.files.ingestAttachment).not.toHaveBeenCalled();
});

it("makes one ingest call and follows the host's status while it wakes", async () => {
  await mount();
  let finish: () => void = () => undefined;
  api.files.ingestAttachment.mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = () => resolve({ fileId: "ab12", name: "a.pdf", pages: 1, chars: 3 });
      }),
  );
  hostBus.emit(STARTING);
  await pickFiles(["a.pdf", "aaa", "application/pdf"]);
  await settled();
  expect(statuses()).toEqual(["starting"]);
  expect(await act(async () => latest.sendMessage("hi"))).toBe(false);
  await act(async () => hostBus.emit(UP));
  expect(statuses()).toEqual(["reading"]);
  await act(async () => finish());
  await settled();
  expect(statuses()).toEqual(["ready"]);
  expect(api.files.ingestAttachment).toHaveBeenCalledTimes(1); // never retried by the page
});

it("cancels the wait and the read when the chip is removed", async () => {
  await mount();
  let signal: AbortSignal | undefined;
  api.files.ingestAttachment.mockImplementation(
    (_input: unknown, options?: { signal?: AbortSignal }) => {
      signal = options?.signal;
      return new Promise(() => undefined);
    },
  );
  await pickFiles(["a.pdf", "aaa", "application/pdf"]);
  await settled();
  expect(signal?.aborted).toBe(false);
  await act(async () => latest.removeAttachment(latest.activePendingAttachments[0] as never));
  expect(signal?.aborted).toBe(true);
  expect(latest.activePendingAttachments).toHaveLength(0);
});

it("cancels the wait for a starting host when the person leaves the chat", async () => {
  await mount();
  api.files.uploadAttachment.mockRejectedValue(
    new ORPCError("SERVICE_UNAVAILABLE", { message: "starting" }),
  );
  await pickFiles(["a.pdf", "aaa", "application/pdf"]);
  await settled();
  expect(statuses()).toEqual(["starting"]);
  for (const root of roots.splice(0)) await act(async () => root.unmount());
  await act(async () => hostBus.emit(UP)); // too late: nobody is waiting any more
  expect(api.files.uploadAttachment).toHaveBeenCalledTimes(1);
});

it("says a slow read is slow, not that the host is starting", async () => {
  await mount();
  api.files.ingestAttachment.mockRejectedValueOnce(
    new ORPCError("GATEWAY_TIMEOUT", { message: "slow" }),
  );
  await pickFiles(["a.pdf", "aaa", "application/pdf"]);
  await settled();
  expect(statuses()).toEqual(["error"]);
  expect(latest.activePendingAttachments[0]?.error).toBe(
    "Reading a.pdf is taking too long. Try again in a moment.",
  );
});

it("gives every picked attachment its own id, even for identical files picked together", async () => {
  await mount();
  await pickFiles(["a.pdf", "aaa", "application/pdf"], ["a.pdf", "aaa", "application/pdf"]);
  await settled();
  const ids = latest.activePendingAttachments.map((a) => a.id);
  expect(new Set(ids).size).toBe(2);
  await pickFiles(["a.pdf", "aaa", "application/pdf"]);
  expect(new Set(latest.activePendingAttachments.map((a) => a.id)).size).toBe(3);
});

it("decides a starting host by the error code, not by words in the message", async () => {
  await mount();
  api.files.ingestAttachment.mockRejectedValueOnce(
    new ORPCError("INTERNAL_SERVER_ERROR", { message: "omnigent read failed (503): starting" }),
  );
  await pickFiles(["a.pdf", "aaa", "application/pdf"]);
  await settled();
  expect(statuses()).toEqual(["error"]);
  expect(latest.activePendingAttachments[0]?.error).not.toContain("Your machine is starting");
});

it("does not wait for an 'up' event that already happened", async () => {
  await mount();
  api.files.uploadAttachment
    .mockRejectedValueOnce(new ORPCError("SERVICE_UNAVAILABLE", { message: "starting" }))
    .mockResolvedValueOnce({
      path: "p/a.pdf",
      name: "a.pdf",
      mimeType: "application/pdf",
      size: 3,
    });
  hostBus.emit(UP); // the host came up in the moment between the call and the wait
  await pickFiles(["a.pdf", "aaa", "application/pdf"]);
  await settled();
  expect(statuses()).toEqual(["ready"]);
  expect(api.files.uploadAttachment).toHaveBeenCalledTimes(2);
});

it("shows the message at once as the engine will record it, sends it, and reads the Conversation", async () => {
  await mount();
  await pickFiles(["a.pdf", "aaa", "application/pdf"]);
  await settled();
  expect(await act(async () => latest.sendMessage("read this"))).toBe(true);
  expect(transcript.beginSend).toHaveBeenCalledWith(
    "Attached file in your workspace: your_files/uploads/2026-10-09/a.pdf (application/pdf, 3 bytes)\n\nread this",
  );
  expect(api.chats.send).toHaveBeenCalledWith({
    botId: BOT,
    text: "read this",
    attachments: [expect.objectContaining({ name: "a.pdf" })],
    folders: undefined,
  });
  expect(transcript.refresh).toHaveBeenCalled();
  expect(transcript.failSend).not.toHaveBeenCalled();
});

it("sends a quote as text and workspace folders as pointers", async () => {
  await mount();
  await act(async () => latest.attachFolder("projects/q3"));
  await act(async () => {
    latest.setReplyTarget({ id: "m1", role: "bot" } as never);
    latest.setReplyQuote("the fee is 2%");
  });
  expect(await act(async () => latest.sendMessage("why?"))).toBe(true);
  expect(api.chats.send).toHaveBeenCalledWith({
    botId: BOT,
    text: "> the fee is 2%\n\nwhy?",
    attachments: undefined,
    folders: ["projects/q3"],
  });
  expect(latest.activePendingFolders).toEqual([]);
});

it("retries a failed send once, quietly", async () => {
  vi.useFakeTimers();
  try {
    await mount();
    api.chats.send.mockRejectedValueOnce(new Error("down")).mockResolvedValueOnce({ ok: true });
    let sent: Promise<boolean> | undefined;
    await act(async () => {
      sent = latest.sendMessage("hello");
      await vi.advanceTimersByTimeAsync(3_000);
    });
    expect(await sent).toBe(true);
    expect(api.chats.send).toHaveBeenCalledTimes(2);
    expect(latest.sendError).toBeNull();
    expect(transcript.failSend).not.toHaveBeenCalled();
  } finally {
    vi.useRealTimers();
  }
});

it("takes the message back and reports it when the retry fails too", async () => {
  vi.useFakeTimers();
  try {
    await mount();
    api.chats.send.mockRejectedValue(new Error("down"));
    let sent: Promise<boolean> | undefined;
    await act(async () => {
      sent = latest.sendMessage("hello");
      await vi.advanceTimersByTimeAsync(3_000);
    });
    expect(await sent).toBe(false);
    expect(api.chats.send).toHaveBeenCalledTimes(2);
    expect(transcript.failSend).toHaveBeenCalledTimes(1);
    expect(latest.sendError).toBeTruthy();
  } finally {
    vi.useRealTimers();
  }
});

it("stops the running turn on the engine, expects no reply, and reads the Conversation again", async () => {
  await mount();
  await act(async () => latest.stopRun());
  expect(api.chats.interrupt).toHaveBeenCalledWith({ botId: BOT });
  expect(transcript.stopWaiting).toHaveBeenCalledTimes(1);
  expect(transcript.refresh).toHaveBeenCalled();
});

it("gives the words back when Stop comes before the message went out, and stops all the same", async () => {
  vi.useFakeTimers();
  try {
    await mount();
    api.chats.send.mockRejectedValueOnce(new Error("down"));
    let sent: Promise<boolean> | undefined;
    await act(async () => {
      sent = latest.sendMessage("hello");
      await vi.advanceTimersByTimeAsync(10);
    });
    // The first post was refused; the retry waits. Stop now: nothing went out.
    expect(latest.sending).toBe(true);
    await act(async () => {
      await latest.stopRun();
      await vi.advanceTimersByTimeAsync(3_000);
    });
    expect(await sent).toBe(false);
    expect(api.chats.send).toHaveBeenCalledTimes(1);
    expect(transcript.failSend).toHaveBeenCalledTimes(1);
    expect(latest.sendError).toBeNull();
    expect(api.chats.interrupt).toHaveBeenCalledWith({ botId: BOT });
    expect(transcript.stopWaiting).toHaveBeenCalledTimes(1);
  } finally {
    vi.useRealTimers();
  }
});

it("lets a message already posted land, keeps it, and then stops the turn it started", async () => {
  let land: (() => void) | undefined;
  api.chats.send.mockImplementation(
    () => new Promise((resolve) => (land = () => resolve({ ok: true }))),
  );
  await mount();
  let sent: Promise<boolean> | undefined;
  await act(async () => {
    sent = latest.sendMessage("hello");
  });
  let stopped: Promise<void> | undefined;
  await act(async () => {
    stopped = latest.stopRun();
  });
  // The stop waits for the post, so it stops the turn that message starts.
  expect(api.chats.interrupt).not.toHaveBeenCalled();
  await act(async () => {
    land?.();
    await stopped;
  });
  expect(api.chats.interrupt).toHaveBeenCalledWith({ botId: BOT });
  // It landed: the transcript keeps it and the composer is not refilled.
  expect(await sent).toBe(true);
  expect(transcript.failSend).not.toHaveBeenCalled();
  expect(transcript.stopWaiting).toHaveBeenCalledTimes(1);
});

it("says it could not stop, apart from any upload status, until cleared", async () => {
  api.chats.interrupt.mockResolvedValueOnce({ delivered: false });
  await mount();
  await act(async () => latest.stopRun());
  expect(latest.stopFailed).toBe(true);
  expect(latest.uploadStatus).toBeNull();
  expect(transcript.stopWaiting).not.toHaveBeenCalled();
  await act(async () => latest.clearStopFailed());
  expect(latest.stopFailed).toBe(false);
});

it("keeps expecting the reply when the stop fails", async () => {
  api.chats.interrupt.mockRejectedValueOnce(new Error("offline"));
  await mount();
  await act(async () => latest.stopRun());
  expect(transcript.stopWaiting).not.toHaveBeenCalled();
});

it("answers an ask on the engine", async () => {
  await mount();
  await act(async () => latest.answerMessage({ id: "ask-1", runId: "r1" } as never, "yes"));
  expect(api.asks.answer).toHaveBeenCalledWith({ askId: "ask-1", runId: "r1", answer: "yes" });
  expect(notifyAsksChanged).toHaveBeenCalledWith(BOT);
});

it("offers the composer note for a reply that failed for a model-layer reason, until dismissed", async () => {
  const failed: ThreadMessage = {
    id: "m9",
    threadId: "conv",
    seq: 9,
    role: "bot",
    blocks: [{ kind: "error", code: "model_key_required" }],
    createdAt: "2026-10-10T10:00:00.000Z",
  };
  shown = [failed];
  await mount();
  expect(latest.failure).toEqual({ id: "m9", code: "model_key_required" });
  await act(async () => latest.dismissComposerError());
  expect(latest.failure).toBeNull();
});
