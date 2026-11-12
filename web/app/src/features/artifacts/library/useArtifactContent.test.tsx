// @vitest-environment jsdom
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ listVersions: vi.fn(), getById: vi.fn() }));
vi.mock("../../../lib/rpc", () => ({ rpc: { artifacts: api } }));

import { useArtifactContent } from "./useArtifactContent";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const ver = (id: string, version: number, origin = "ai") => ({
  id,
  version,
  name: "mock_data.csv",
  createdAt: "",
  origin,
});
const file = (id: string, version: number) => ({
  id,
  version,
  name: "mock_data.csv",
  mimeType: "text/csv",
  contentBase64: "",
});

let latest: ReturnType<typeof useArtifactContent>;
function Probe() {
  latest = useArtifactContent("a1");
  return null;
}

const root = document.createElement("div");
const react = createRoot(root);
const render = () =>
  act(async () => {
    react.render(createElement(Probe));
  });
const tick = () => act(async () => vi.advanceTimersByTimeAsync(4000));

afterEach(() => {
  vi.clearAllMocks();
  vi.useRealTimers();
});

it("polls while open, follows a newer version, and stays on one the person picked", async () => {
  api.listVersions.mockResolvedValue([ver("a1", 1)]);
  api.getById.mockImplementation(async ({ artifactId }: { artifactId: string }) =>
    file(artifactId, artifactId === "a2" ? 2 : 1),
  );
  vi.useFakeTimers();
  await render();
  expect(latest.versionId).toBe("a1");

  api.listVersions.mockResolvedValue([ver("a2", 2), ver("a1", 1)]);
  await tick();
  expect(latest.versionId).toBe("a2");
  expect(latest.status).toBe("ready");
  expect(latest.versions).toHaveLength(2);
  expect(latest.arrived?.version).toBe(2);
  expect(latest.newer).toBeNull();

  await act(async () => latest.selectVersion("a1"));
  expect(latest.versionId).toBe("a1");
  api.listVersions.mockResolvedValue([ver("a3", 3), ver("a2", 2), ver("a1", 1)]);
  await tick();
  expect(latest.versionId).toBe("a1");
  expect(latest.newer?.id).toBe("a3");

  await act(async () => latest.showNewest());
  expect(latest.versionId).toBe("a3");
  expect(latest.newer).toBeNull();
});
