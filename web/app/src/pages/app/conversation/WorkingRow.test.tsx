// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { WorkingRow } from "./WorkingRow";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../../test/i18n")).withI18nRoot(
    await orig<typeof import("react-dom/client")>(),
  ),
);
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root;
let host: HTMLDivElement;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-01-01T00:05:00Z"));
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.useRealTimers();
});

it("counts from the run's start, so a remount keeps the elapsed time", async () => {
  const startedAt = Date.parse("2026-01-01T00:03:00Z");
  await act(async () => root.render(<WorkingRow label="Thinking…" since={startedAt} />));
  const first = host.textContent;
  await act(async () => root.unmount());
  root = createRoot(host);
  await act(async () => root.render(<WorkingRow label="Thinking…" since={startedAt} />));
  expect(host.textContent).toBe(first);
  expect(host.textContent).toContain("2:00");
});
