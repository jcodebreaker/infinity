// @vitest-environment jsdom

import type { Automation } from "@superchat/contracts";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { type AutomationsWire, useAutomations } from "./useAutomations";

vi.mock("../../lib/rpc", () => ({ rpc: {} }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const automation = (lastRunStatus: string | null) =>
  ({ id: "a1", name: "Brief", lastRunAt: null, lastRunStatus }) as unknown as Automation;

let root: Root;
let latest: ReturnType<typeof useAutomations> | undefined;

function Probe({ wire }: { wire: AutomationsWire }) {
  latest = useAutomations("bot-1", true, wire);
  return null;
}

beforeEach(() => {
  vi.useFakeTimers();
  root = createRoot(document.createElement("div"));
});

afterEach(() => {
  act(() => root.unmount());
  vi.useRealTimers();
});

it("follows a run until it ends, then stops asking", async () => {
  const list = vi
    .fn()
    .mockResolvedValueOnce([automation(null)])
    .mockResolvedValueOnce([automation("running")])
    .mockResolvedValueOnce([automation("running")])
    .mockResolvedValue([automation("succeeded")]);
  const wire = {
    list,
    system: vi.fn().mockResolvedValue([]),
    run: vi.fn().mockResolvedValue(undefined),
  } as unknown as AutomationsWire;
  await act(async () => root.render(<Probe wire={wire} />));
  expect(list).toHaveBeenCalledTimes(1);

  await act(async () => latest?.runNow("a1"));
  expect(list).toHaveBeenCalledTimes(2);

  await act(async () => vi.advanceTimersByTimeAsync(3_000));
  await act(async () => vi.advanceTimersByTimeAsync(3_000));
  expect(list).toHaveBeenCalledTimes(4);
  expect(latest?.state).toEqual({ status: "ready", automations: [automation("succeeded")] });

  await act(async () => vi.advanceTimersByTimeAsync(30_000));
  expect(list).toHaveBeenCalledTimes(4);
});
