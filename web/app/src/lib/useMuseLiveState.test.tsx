// @vitest-environment jsdom

import type { ThreadMessage } from "@superchat/contracts";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  type MuseLiveRun,
  type MuseLiveState,
  useMuseLiveState,
  useTurnRuns,
  waitingKey,
} from "./useMuseLiveState";

vi.mock("react-dom/client", async (orig) =>
  (await import("../test/i18n")).withI18nRoot(await orig<typeof import("react-dom/client")>()),
);

const api = vi.hoisted(() => ({ host: { status: vi.fn() } }));
vi.mock("./rpc", () => ({ rpc: api }));
const notifyAsksChanged = vi.hoisted(() => vi.fn());
vi.mock("../features/approvals", () => ({
  useAsks: () => ({ count: 0 }),
  notifyAsksChanged,
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root;
let host: HTMLDivElement;
let latest: MuseLiveState | undefined;

let probeMessages: ThreadMessage[] = [];

function Probe({ status }: { status: MuseLiveRun["status"] }) {
  latest = useMuseLiveState({
    botId: "bot-1",
    runs: [{ id: "r1", status }],
    messages: probeMessages,
  });
  return null;
}

async function mount(status: MuseLiveRun["status"]) {
  await act(async () => {
    root.render(<Probe status={status} />);
  });
}

beforeEach(() => {
  probeMessages = [];
  host = document.createElement("div");
  root = createRoot(host);
  latest = undefined;
  api.host.status.mockReset();
});
afterEach(() => act(() => root.unmount()));

it("says the host is starting while a running turn waits on its launch", async () => {
  api.host.status.mockResolvedValue({ launch: { stage: "starting" } });
  await mount("running");
  expect(latest?.label).toBe("Starting your machine…");
});

it("keeps the usual label when there is no launch in progress", async () => {
  api.host.status.mockResolvedValue({});
  await mount("leased");
  expect(latest?.label).toBe("Thinking…");
  await mount("running");
  expect(latest?.label).toBe("Working…");
});

it("does not call a failed launch a start", async () => {
  api.host.status.mockResolvedValue({ launch: { stage: "failed" } });
  await mount("running");
  expect(latest?.label).toBe("Working…");
});

it("does not read the host while idle", async () => {
  await mount("completed");
  expect(api.host.status).not.toHaveBeenCalled();
  expect(latest?.label).toBeUndefined();
});

it("refetches the open Asks when a run starts waiting on the person", async () => {
  api.host.status.mockResolvedValue({});
  await mount("running");
  notifyAsksChanged.mockClear();
  await mount("running");
  expect(notifyAsksChanged).not.toHaveBeenCalled();
  await mount("waiting_input");
  expect(notifyAsksChanged).toHaveBeenCalledWith("bot-1");
});

it("keys the waiting set by waiting runs and open ask blocks", () => {
  const ask = (status: string) =>
    ({ id: "m1", blocks: [{ kind: "text" }, { kind: "ask", status }] }) as never;
  expect(waitingKey([{ id: "r1", status: "running" }], [ask("answered")])).toBe("");
  expect(waitingKey([{ id: "r1", status: "waiting_input" }], [ask("pending")])).toBe("r1,m1#1");
});

const reply = (role: ThreadMessage["role"], blocks: ThreadMessage["blocks"]): ThreadMessage => ({
  id: `${role}-${blocks.length}`,
  threadId: "conv",
  seq: 0,
  role,
  blocks,
  createdAt: "2026-10-10T10:00:00.000Z",
});

it("names the step the Muse is on from what it has written since the person's last message", async () => {
  api.host.status.mockResolvedValue({});
  probeMessages = [
    reply("user", [{ kind: "text", text: "find it" }]),
    reply("bot", [{ kind: "steps", steps: [{ label: "Search the web" }] } as never]),
  ];
  await mount("running");
  expect(latest?.label).toBe("Searching the web…");
  // A step from the turn before the person's message is not this turn's.
  probeMessages = [
    reply("bot", [{ kind: "steps", steps: [{ label: "Search the web" }] } as never]),
    reply("user", [{ kind: "text", text: "and again" }]),
  ];
  await mount("running");
  expect(latest?.label).toBe("Working…");
});

it("runs a turn only while it is running, the same run while it does", async () => {
  let runs: MuseLiveRun[] = [];
  function Turn({ running }: { running: boolean }) {
    runs = useTurnRuns("conversation:bot-1", running);
    return null;
  }
  await act(async () => root.render(<Turn running={false} />));
  expect(runs).toEqual([]);
  await act(async () => root.render(<Turn running />));
  const first = runs[0];
  expect(first).toMatchObject({ id: "conversation:bot-1", status: "running" });
  await act(async () => root.render(<Turn running />));
  expect(runs[0]).toBe(first);
  await act(async () => root.render(<Turn running={false} />));
  expect(runs).toEqual([]);
});
