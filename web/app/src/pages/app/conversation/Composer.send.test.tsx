// @vitest-environment jsdom

import type { Bot, HostStatus } from "@superchat/contracts";
import { act, type ReactNode } from "react";
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
vi.mock("@lingui/react/macro", () => {
  const t = (parts: TemplateStringsArray, ...values: unknown[]) =>
    parts.reduce((acc, part, i) => `${acc}${part}${values[i] ?? ""}`, "");
  return {
    useLingui: () => ({ t, i18n: { locale: "en" } }),
    Trans: ({ children }: { children: ReactNode }) => children,
  };
});
vi.mock("../../../lib/browser-notifications", () => ({
  requestBrowserNotificationPermission: () => null,
}));
vi.mock("../intro", () => ({ markFirstRunSeen: vi.fn() }));
vi.stubGlobal(
  "ResizeObserver",
  class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
);

import { deriveWorkState, IDLE_TURN } from "../../../features/activity/workState";
import { Composer } from "./Composer";
import { useComposerSend } from "./useComposerSend";

const BOT = "b".repeat(32);
const botRef = { current: BOT as string | undefined };
const roots: Root[] = [];
let latest: ReturnType<typeof useComposerSend>;
/** Whether the Conversation reads as running (Stop shows only then). */
let running = false;
const transcript = {
  beginSend: vi.fn(),
  failSend: vi.fn(),
  stopWaiting: vi.fn(),
  refresh: vi.fn(),
};

/** The real hook wired to the real Composer, the way Shell does it. */
function Wired() {
  latest = useComposerSend({
    active: { id: BOT } as Bot,
    activeBotId: botRef,
    userId: "u1",
    messages: [],
    transcript,
    botsRef: { current: [] },
    flushPendingBrowserNotifications: () => undefined,
  });
  return (
    <Composer
      phase={
        deriveWorkState({
          turn: { ...IDLE_TURN, running },
          sending: latest.sending,
          stopping: latest.stopping,
          activities: [],
          turnStartedAt: null,
        }).phase
      }
      stopFailed={latest.stopFailed}
      onStop={latest.stopRun}
      pendingAttachments={latest.activePendingAttachments}
      uploadStatus={latest.uploadStatus}
      fileInputRef={latest.fileInputRef}
      onAttachmentPick={latest.onAttachmentPick}
      onRemoveAttachment={latest.removeAttachment}
      onRetryAttachment={latest.retryAttachment}
      onSend={latest.sendMessage}
    />
  );
}

async function mountWithAttachmentAndText(text: string) {
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  roots.push(root);
  await act(async () => root.render(<Wired />));
  const file = new File(["aaa"], "vendor-review.pdf", { type: "application/pdf", lastModified: 1 });
  await act(async () =>
    latest.onAttachmentPick(Object.assign([file], { item: () => file }) as never),
  );
  await act(async () => new Promise((resolve) => setTimeout(resolve, 50)));
  const box = host.querySelector("textarea") as HTMLTextAreaElement;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(box, text);
    box.dispatchEvent(new Event("input", { bubbles: true }));
  });
  return { host, box };
}

beforeEach(() => {
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
  api.files.ingestAttachment.mockReset().mockResolvedValue({
    fileId: "ab12",
    name: "vendor-review.pdf",
    pages: 2,
    chars: 40,
  });
  api.chats.send.mockReset().mockResolvedValue({ ok: true });
  api.chats.interrupt.mockReset().mockResolvedValue({ delivered: true });
  running = false;
});
afterEach(async () => {
  for (const root of roots.splice(0)) await act(async () => root.unmount());
  document.body.innerHTML = "";
});

const settle = () => act(async () => new Promise((resolve) => setTimeout(resolve, 150)));

async function press(box: HTMLTextAreaElement) {
  await act(async () => {
    box.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  });
  await settle();
}

async function click(host: HTMLElement) {
  await act(async () => (host.querySelector('button[aria-label="Send"]') as HTMLElement).click());
  await settle();
}

for (const how of ["Enter", "Send button"] as const) {
  it(`${how} sends the question with the attachment that was read when it was picked`, async () => {
    const { host, box } = await mountWithAttachmentAndText("what is the Northwind fee?");
    if (how === "Enter") await press(box);
    else await click(host);
    expect(api.files.uploadAttachment).toHaveBeenCalledTimes(1);
    expect(api.files.ingestAttachment).toHaveBeenCalledTimes(1);
    expect(api.chats.send).toHaveBeenCalledTimes(1);
    expect(api.chats.send.mock.calls[0]?.[0]).toMatchObject({
      botId: BOT,
      text: "what is the Northwind fee?",
    });
    expect(latest.activePendingAttachments).toHaveLength(0);
    expect(box.value).toBe("");
  });
}

it("gives the draft back when sendMessage returns early without sending", async () => {
  const { host, box } = await mountWithAttachmentAndText("keep me");
  // The target vanished (thread switch): sendMessage bails out before it sends anything.
  botRef.current = undefined;
  try {
    await press(box);
  } finally {
    botRef.current = BOT;
  }
  expect(api.chats.send).not.toHaveBeenCalled();
  expect(box.value).toBe("keep me");
  expect(host.textContent).toContain("vendor-review.pdf");
});

it("shows Reading on the chip and keeps Send disabled until the file is ready", async () => {
  let release: () => void = () => undefined;
  api.files.ingestAttachment.mockImplementation(
    () =>
      new Promise((resolve) => {
        release = () => resolve({ fileId: "ab12", name: "vendor-review.pdf", pages: 2, chars: 40 });
      }),
  );
  const { host, box } = await mountWithAttachmentAndText("what is the fee?");
  const chip = host.querySelector('[data-testid="composer-attachment"]') as HTMLElement;
  expect(chip.dataset.status).toBe("reading");
  expect(chip.textContent).toContain("Reading vendor-review.pdf…");
  const sendButton = host.querySelector('button[aria-label="Send"]') as HTMLButtonElement;
  expect(sendButton.disabled).toBe(true);
  await press(box);
  expect(api.chats.send).not.toHaveBeenCalled();
  expect(box.value).toBe("what is the fee?");
  await act(async () => release());
  await settle();
  expect(chip.dataset.status).toBe("ready");
  expect(sendButton.disabled).toBe(false);
});

it("shows Starting your machine on the chip while the host wakes", async () => {
  api.files.ingestAttachment.mockImplementation(() => new Promise(() => undefined));
  hostBus.emit(STARTING);
  const { host } = await mountWithAttachmentAndText("hi");
  const chip = host.querySelector('[data-testid="composer-attachment"]') as HTMLElement;
  expect(chip.dataset.status).toBe("starting");
  expect(chip.textContent).toContain("Starting your machine…");
  expect(chip.textContent).not.toContain("Uploading");
  expect((host.querySelector('button[aria-label="Send"]') as HTMLButtonElement).disabled).toBe(
    true,
  );
});

it("shows a failure on the chip with Retry, and never loses the draft", async () => {
  api.files.ingestAttachment.mockRejectedValueOnce(new Error("boom"));
  const { host, box } = await mountWithAttachmentAndText("keep this");
  const chip = host.querySelector('[data-testid="composer-attachment"]') as HTMLElement;
  expect(chip.dataset.status).toBe("error");
  expect((host.querySelector('button[aria-label="Send"]') as HTMLButtonElement).disabled).toBe(
    true,
  );
  expect(box.value).toBe("keep this");
  const retry = Array.from(chip.querySelectorAll("button")).find((b) => b.textContent === "Retry");
  await act(async () => retry?.click());
  await settle();
  expect(chip.dataset.status).toBe("ready");
});

/** A send that lands when the test says so. */
function heldSend() {
  const held: { land: () => void } = { land: () => undefined };
  api.chats.send.mockImplementation(
    () => new Promise((resolve) => (held.land = () => resolve({ ok: true }))),
  );
  return held;
}

const stopButton = (host: HTMLElement) =>
  host.querySelector('button[aria-label="Stop"]') as HTMLButtonElement;

it("keeps Stop available while a send is on its way, and stops the turn that message starts", async () => {
  running = true;
  const held = heldSend();
  const { host } = await mountWithAttachmentAndText("long job");
  await act(async () => (host.querySelector('button[aria-label="Send"]') as HTMLElement).click());
  await settle();
  expect(latest.sending).toBe(true);
  expect(stopButton(host).disabled).toBe(false);

  await act(async () => stopButton(host).click());
  await settle();
  // The message is on its way: the Stop waits for it, held meanwhile.
  expect(stopButton(host).disabled).toBe(true);
  expect(api.chats.interrupt).not.toHaveBeenCalled();

  await act(async () => held.land());
  await settle();
  expect(api.chats.interrupt).toHaveBeenCalledWith({ botId: BOT });
  expect(latest.sending).toBe(false);
  expect(latest.sendError).toBeNull();
  expect(transcript.stopWaiting).toHaveBeenCalled();
  // It went out: the draft is not given back.
  expect((host.querySelector("textarea") as HTMLTextAreaElement).value).toBe("");
});

it("says so quietly when the stop did not land", async () => {
  running = true;
  api.chats.interrupt.mockResolvedValue({ delivered: false });
  const { host } = await mountWithAttachmentAndText("");
  transcript.stopWaiting.mockClear();
  await act(async () => stopButton(host).click());
  await settle();
  expect(host.querySelector('[data-testid="composer-upload-status"]')?.textContent).toBe(
    "Couldn't stop",
  );
  expect(transcript.stopWaiting).not.toHaveBeenCalled();
});
