// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  chats: { send: vi.fn(), interrupt: vi.fn(), transcript: vi.fn() },
  artifacts: { listVersions: vi.fn() },
}));
vi.mock("../../../lib/rpc", () => ({ rpc: api }));
vi.mock("../../../lib/family-stream", () => ({ watchFamily: () => () => undefined }));
vi.mock("react-dom/client", async (orig) =>
  (await import("../../../test/i18n")).withI18nRoot(
    await orig<typeof import("react-dom/client")>(),
  ),
);

import { useArtifactAsk } from "./useArtifactAsk";

let latest: ReturnType<typeof useArtifactAsk>;
const artifact = { id: "art_1", name: "Deck", version: 1, botId: "bot-1" };
function Harness() {
  latest = useArtifactAsk({ artifact, contextBlock: () => "" });
  return null;
}
const roots: Root[] = [];

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  api.chats.send.mockReset().mockResolvedValue({ ok: true });
  api.chats.interrupt.mockReset();
});
afterEach(async () => {
  for (const root of roots.splice(0)) await act(async () => root.unmount());
});

async function askThenStop() {
  const root = createRoot(document.createElement("div"));
  roots.push(root);
  await act(async () => root.render(<Harness />));
  await act(async () => {
    await latest.send("shorter");
  });
  await act(async () => latest.stop());
}

it("clears the ask when the stop lands", async () => {
  api.chats.interrupt.mockResolvedValue({ delivered: true });
  await askThenStop();
  expect(latest.status).toEqual({ kind: "idle" });
});

it("says so when the stop did not land, instead of swallowing it", async () => {
  api.chats.interrupt.mockResolvedValue({ delivered: false });
  await askThenStop();
  expect(latest.status).toEqual({ kind: "failed", asked: "shorter", message: "Couldn't stop" });
});

it("says so when the stop request fails", async () => {
  api.chats.interrupt.mockRejectedValue(new Error("offline"));
  await askThenStop();
  expect(latest.status).toMatchObject({ kind: "failed", message: "Couldn't stop" });
});

it("in a studio follows the studio's own running state, not the stream", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  try {
    api.artifacts.listVersions.mockResolvedValue([{ version: 1 }]);
    api.chats.transcript.mockResolvedValue({
      messages: [{ role: "bot", blocks: [{ kind: "text", text: "Nothing to change." }] }],
    });
    const onSent = vi.fn();
    const studio = { ...artifact, chatId: "studio-1" };
    function Studio({ running }: { running: boolean }) {
      latest = useArtifactAsk({
        artifact: studio,
        contextBlock: () => "",
        turn: { running, onSent },
      });
      return null;
    }
    const root = createRoot(document.createElement("div"));
    roots.push(root);
    await act(async () => root.render(<Studio running={false} />));
    await act(async () => {
      await latest.send("shorter");
    });
    expect(onSent).toHaveBeenCalledTimes(1);
    await act(async () => root.render(<Studio running />));
    expect(latest.status).toEqual({ kind: "editing", asked: "shorter" });
    // The studio's turn ended with no new version: its reply, after the grace.
    await act(async () => root.render(<Studio running={false} />));
    await act(async () => vi.advanceTimersByTime(6000));
    await act(async () => {});
    expect(latest.status).toEqual({
      kind: "replied",
      asked: "shorter",
      reply: "Nothing to change.",
    });
    expect(api.chats.transcript).toHaveBeenCalledWith({ botId: "bot-1", chatId: "studio-1" });
  } finally {
    vi.useRealTimers();
  }
});
