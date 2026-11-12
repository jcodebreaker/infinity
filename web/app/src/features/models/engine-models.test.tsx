// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

const status = vi.hoisted(() => vi.fn());
vi.mock("../../lib/rpc", () => ({ rpc: { engineModels: { status } } }));

import { resetEngineModelsListeners, useEngineModelsStatus } from "./engine-models";

afterEach(resetEngineModelsListeners);

it("a refresh in one place asks again everywhere the status is shown", async () => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  status.mockResolvedValue({ enabled: true, harnesses: [], ready: true });
  const refreshes: (() => void)[] = [];
  function Probe() {
    const [, refresh] = useEngineModelsStatus();
    refreshes.push(refresh);
    return null;
  }
  const container = document.createElement("div");
  const root = createRoot(container);
  await act(async () =>
    root.render(
      <>
        <Probe />
        <Probe />
      </>,
    ),
  );
  expect(status).toHaveBeenCalledTimes(2);
  await act(async () => refreshes[0]?.());
  expect(status).toHaveBeenCalledTimes(4);
  await act(async () => root.unmount());
});
