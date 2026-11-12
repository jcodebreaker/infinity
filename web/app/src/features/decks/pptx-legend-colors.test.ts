// @vitest-environment jsdom

// The preview renderer (@aiden0z/pptx-renderer) drew chart legends from its theme palette. A pie or
// doughnut legend lists categories, and so does a varyColors bar chart's, but their colours live on
// the data points (<c:dPt>); patches/@aiden0z__pptx-renderer@1.3.0.patch makes each legend entry
// carry its point's colour. This renders fixtures/legend-colors.pptx (make-legend-fixture.cjs) with
// the real renderer and checks every legend swatch against the colours written in the chart parts.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { PptxViewer } from "@aiden0z/pptx-renderer";
import JSZip from "jszip";
import { afterEach, beforeAll, expect, it } from "vitest";
import { pptxForPreview } from "./pptx-normalize";

// jsdom has no canvas: ECharts paints into a stub, the legend is plain DOM and is what is asserted.
const canvas2d: object = new Proxy(() => undefined, {
  get: (_target, key) =>
    key === Symbol.toPrimitive
      ? () => 0
      : key === "measureText"
        ? () => ({ width: 8 })
        : key === "canvas"
          ? {}
          : canvas2d,
  set: () => true,
  apply: () => canvas2d,
});

beforeAll(() => {
  HTMLCanvasElement.prototype.getContext = (() => canvas2d) as never;
});
afterEach(() => {
  document.body.innerHTML = "";
});

const fixture = () => new Uint8Array(readFileSync(join(__dirname, "fixtures/legend-colors.pptx")));

/** The slice/bar colours the file itself writes, in point order (`<c:dPt>` solid fills). */
async function pointColors(bytes: Uint8Array, chart: number): Promise<string[]> {
  const zip = await JSZip.loadAsync(bytes);
  const xml = (await zip.file(`ppt/charts/chart${chart}.xml`)?.async("string")) ?? "";
  return [...xml.matchAll(/<c:dPt>[\s\S]*?<a:srgbClr val="([0-9A-F]{6})"/g)].map((m) => `#${m[1]}`);
}

/** Every chart legend of the rendered deck: its (label, swatch fill) entries, in slide order. */
async function legends(bytes: Uint8Array): Promise<{ label: string; fill: string }[][]> {
  const host = document.createElement("div");
  host.style.width = "1200px";
  host.style.height = "700px";
  document.body.append(host);
  const viewer = await PptxViewer.open(await pptxForPreview(bytes), host, { fitMode: "contain" });
  try {
    return [...host.querySelectorAll(".pptx-chart-custom-legend")].map((legend) =>
      [...legend.children].map((entry) => ({
        label: entry.querySelector("span")?.textContent ?? "",
        fill: entry.querySelector("svg rect, svg path, svg line")?.getAttribute("fill") ?? "",
      })),
    );
  } finally {
    viewer.destroy();
  }
}

/** Sets `<c:varyColors val="1"/>` in a chart part of the fixture (what a single-series bar with varied colours writes). */
async function withVaryColors(bytes: Uint8Array, chart: number): Promise<Uint8Array> {
  const zip = await JSZip.loadAsync(bytes);
  const name = `ppt/charts/chart${chart}.xml`;
  const xml = (await zip.file(name)?.async("string")) ?? "";
  zip.file(name, xml.replace('<c:varyColors val="0"/>', '<c:varyColors val="1"/>'));
  return zip.generateAsync({ type: "uint8array" });
}

const categories = ["North", "South", "East", "West"];

it("draws a pie legend in the slice colours", async () => {
  const bytes = fixture();
  const slice = await pointColors(bytes, 1);
  expect(slice).toHaveLength(4);
  const [pie] = await legends(bytes);
  expect(pie?.map((e) => e.label)).toEqual(categories);
  expect(pie?.map((e) => e.fill)).toEqual(slice);
});

it("draws a doughnut legend in the slice colours", async () => {
  const bytes = fixture();
  const slice = await pointColors(bytes, 2);
  const doughnut = (await legends(bytes))[1];
  expect(doughnut?.map((e) => e.label)).toEqual(categories);
  expect(doughnut?.map((e) => e.fill)).toEqual(slice);
});

it("keeps one series entry for a bar chart that does not vary colours", async () => {
  const bytes = fixture();
  const bar = (await legends(bytes))[2];
  expect(bar?.map((e) => e.label)).toEqual(["Revenue"]);
});

it("lists a varyColors bar chart's categories in the colours of their bars", async () => {
  const bytes = await withVaryColors(fixture(), 3);
  const bars = await pointColors(bytes, 3);
  expect(bars).toHaveLength(4);
  const bar = (await legends(bytes))[2];
  expect(bar?.map((e) => e.label)).toEqual(categories);
  expect(bar?.map((e) => e.fill)).toEqual(bars);
});
