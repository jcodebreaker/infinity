// @vitest-environment jsdom

import { i18n } from "@lingui/core";
import { I18nProvider } from "@lingui/react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ getById: vi.fn() }));
vi.mock("../../lib/rpc", () => ({ rpc: { artifacts: api } }));
vi.mock("../../lib/artifact-open", () => ({
  decodeArtifactBase64: (base64: string) => Uint8Array.from(atob(base64), (c) => c.charCodeAt(0)),
}));

import { FileStudioProvider } from "../../components/cards/context";
import { chartsExtension } from "./extension";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  api.getById.mockReset();
});
afterEach(() => host.remove());

i18n.loadAndActivate({ locale: "en", messages: {} });

function render(node: React.ReactNode) {
  const root = createRoot(host);
  act(() => root.render(<I18nProvider i18n={i18n}>{node}</I18nProvider>));
  return root;
}

it("shows a dashboard page live at full width, as tall as the page says, with Open", async () => {
  const page = "<!doctype html><title>Sales</title><h1>Sales 2025</h1>";
  api.getById.mockResolvedValue({ contentBase64: btoa(page) });
  const open = vi.fn();
  render(
    <FileStudioProvider value={{ open } as never}>
      {chartsExtension.result?.({
        artifactId: "d1",
        name: "sales.dashboard.html",
        version: 7,
        data: {},
      })}
    </FileStudioProvider>,
  );
  await act(async () => {});
  const card = host.querySelector("[data-dashboard-page]") as HTMLElement;
  expect(card.className).toContain("w-[48rem]");
  const frame = card.querySelector("iframe") as HTMLIFrameElement;
  expect(frame.getAttribute("sandbox")).toBe("allow-scripts");
  expect(frame.getAttribute("srcdoc")).toContain("Sales 2025");

  const box = frame.parentElement as HTMLElement;
  act(() => {
    window.dispatchEvent(
      new MessageEvent("message", {
        data: { type: "superchat:dashboard-height", height: 910 },
        source: frame.contentWindow,
      }),
    );
  });
  expect(box.style.height).toBe("910px");
  // Another window cannot resize it.
  act(() => {
    window.dispatchEvent(
      new MessageEvent("message", { data: { type: "superchat:dashboard-height", height: 20 } }),
    );
  });
  expect(box.style.height).toBe("910px");

  act(() => (card.querySelector("button[aria-label^='Open']") as HTMLButtonElement).click());
  expect(open).toHaveBeenCalledWith("d1");
  expect(chartsExtension.card({ name: "sales.dashboard.html" } as never)?.meta).toBe("Dashboard");
  expect(chartsExtension.result?.({ artifactId: "x", name: "page.html", data: {} })).toBeNull();
});

it("shows a dashboard page whose request failed with Retry, and loads it again", async () => {
  api.getById.mockRejectedValueOnce(new Error("offline"));
  render(chartsExtension.result?.({ artifactId: "d2", name: "q3.dashboard.html", data: {} }));
  await act(async () => {});
  expect(host.querySelector("[data-chart-state='error']")).not.toBeNull();

  api.getById.mockResolvedValue({ contentBase64: btoa("<h1>Q3</h1>") });
  const retry = [...host.querySelectorAll("button")].find((b) => b.textContent === "Retry");
  await act(async () => retry?.click());
  expect(host.querySelector("iframe")?.getAttribute("srcdoc")).toContain("Q3");
});

it("shows a chart from a retired format as a one-line note", () => {
  for (const name of ["rev.chart.json", "sales.dashboard.json"]) {
    render(chartsExtension.result?.({ artifactId: "c", name, data: {} }));
    expect(host.querySelector("[data-legacy-chart]")?.textContent).toBe(
      "This chart was made with an older version",
    );
    host.replaceChildren();
  }
  expect(chartsExtension.card({ name: "rev.chart.json" } as never)).toBeNull();
});
