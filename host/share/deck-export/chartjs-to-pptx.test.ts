// Unit tests for the Chart.js -> PptxGenJS mapping in deck-capture-page.js (Superchat addition). The
// real-Chromium round trip (every type exported, read back as a .pptx) is
// engine/tests/superchat/decks/test_export_charts.py.
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { chartJsToPptx, parseChartColor } from "./deck-capture-page.js";

type Dataset = Record<string, unknown>;
const fakeCanvas = { parentElement: null } as unknown as HTMLCanvasElement;

function chart(config: {
  type: string;
  labels?: string[];
  datasets: Dataset[];
  options?: Record<string, unknown>;
}) {
  return {
    config: { type: config.type },
    data: { labels: config.labels ?? ["Q1", "Q2", "Q3"], datasets: config.datasets },
    options: config.options ?? {},
    getDatasetMeta: () => null,
  };
}

beforeEach(() => {
  vi.stubGlobal("window", { Chart: { defaults: { font: { family: "Helvetica" } } } });
  vi.stubGlobal("getComputedStyle", () => ({
    backgroundColor: "rgb(255, 255, 255)",
    fontFamily: '"Inter", sans-serif',
  }));
});
afterEach(() => {
  vi.unstubAllGlobals();
});

const bar = (extra: Dataset = {}, options: Record<string, unknown> = {}) =>
  chart({
    type: "bar",
    datasets: [{ label: "Sales", data: [1, 2, 3], backgroundColor: "#1e2bfa", ...extra }],
    options,
  });

describe("colors", () => {
  test("hex, short hex, hex with alpha and rgb() parse to a hex and an alpha", () => {
    expect(parseChartColor("#1e2bfa", "c")).toEqual({ hex: "1E2BFA", alpha: 1 });
    expect(parseChartColor("#abc", "c")).toEqual({ hex: "AABBCC", alpha: 1 });
    const faded = parseChartColor("#1e2bfa22", "c");
    expect(faded.hex).toBe("1E2BFA");
    expect(faded.alpha).toBeCloseTo(0.133, 2);
    expect(parseChartColor("rgba(0, 0, 0, 0.1)", "c")).toEqual({ hex: "000000", alpha: 0.1 });
  });
  test("a gradient or a function result is refused with a sentence", () => {
    expect(() => parseChartColor({} as unknown as string, "The fill")).toThrow(/plain CSS color/);
  });
});

describe("types with no PowerPoint chart", () => {
  test("polarArea is refused and the message tells the Muse what to use", () => {
    const polar = chart({ type: "polarArea", datasets: [{ label: "x", data: [1, 2, 3] }] });
    expect(() => chartJsToPptx(polar, fakeCanvas)).toThrow(
      /polarArea chart has no native PowerPoint equivalent.*table/s,
    );
  });
  test("scatter cannot share a chart with bars", () => {
    const mixed = chart({
      type: "bar",
      datasets: [
        { type: "bar", label: "a", data: [1, 2, 3], backgroundColor: "#111111" },
        { type: "scatter", label: "b", data: [{ x: 1, y: 1 }], backgroundColor: "#222222" },
      ],
    });
    expect(() => chartJsToPptx(mixed, fakeCanvas)).toThrow(/cannot share a chart/);
  });
  test("a doughnut with several rings and a gauge are refused", () => {
    const rings = chart({
      type: "doughnut",
      datasets: [
        { label: "a", data: [1, 2, 3], backgroundColor: ["#111111", "#222222", "#333333"] },
        { label: "b", data: [1, 2, 3], backgroundColor: ["#111111", "#222222", "#333333"] },
      ],
    });
    expect(() => chartJsToPptx(rings, fakeCanvas)).toThrow(/one series in PowerPoint/);
    const gauge = chart({
      type: "doughnut",
      datasets: [{ label: "a", data: [1, 2], backgroundColor: ["#111111", "#222222"] }],
      options: { circumference: 180 },
    });
    expect(() => chartJsToPptx(gauge, fakeCanvas)).toThrow(/gauge/);
  });
  test("a stacked line, three value axes, and bars with several colors among datasets are refused", () => {
    const stackedLine = chart({
      type: "line",
      datasets: [{ label: "a", data: [1, 2, 3], borderColor: "#111111" }],
      options: { scales: { x: { stacked: true }, y: { stacked: true } } },
    });
    expect(() => chartJsToPptx(stackedLine, fakeCanvas)).toThrow(/stacked line/);
    const manyAxes = chart({
      type: "bar",
      datasets: ["y", "y1", "y2"].map((id) => ({
        label: id,
        data: [1, 2, 3],
        yAxisID: id,
        backgroundColor: "#111111",
      })),
    });
    expect(() => chartJsToPptx(manyAxes, fakeCanvas)).toThrow(/three or more/);
    const shared = chart({
      type: "bar",
      datasets: [
        { label: "a", data: [1, 2, 3], backgroundColor: ["#111111", "#222222", "#333333"] },
        { label: "b", data: [1, 2, 3], backgroundColor: "#444444" },
      ],
    });
    expect(() => chartJsToPptx(shared, fakeCanvas)).toThrow(/one color/);
  });
  test("a scatter series that repeats an x value cannot sit on a shared x column", () => {
    const scatter = chart({
      type: "scatter",
      datasets: [
        { label: "a", data: [{ x: 1, y: 1 }], backgroundColor: "#111111" },
        {
          label: "b",
          data: [
            { x: 2, y: 1 },
            { x: 2, y: 3 },
          ],
          backgroundColor: "#222222",
        },
      ],
    });
    expect(() => chartJsToPptx(scatter, fakeCanvas)).toThrow(/repeats an x value/);
  });
});

describe("what is read from the config", () => {
  test("a number format on the value axis becomes an Excel format code", () => {
    const spec = chartJsToPptx(
      bar(
        {},
        {
          scales: {
            y: {
              ticks: { format: { style: "currency", currency: "USD", minimumFractionDigits: 0 } },
            },
          },
        },
      ),
      fakeCanvas,
    );
    expect(spec.options.valAxisLabelFormatCode).toBe('"$"#,##0');
  });
  test("a horizontal bar reads its value axis from x and flips the category order", () => {
    const spec = chartJsToPptx(
      bar({}, { indexAxis: "y", scales: { x: { min: 0, max: 10, ticks: { stepSize: 5 } } } }),
      fakeCanvas,
    );
    expect(spec.types[0].options.barDir).toBe("bar");
    expect(spec.options).toMatchObject({
      valAxisMinVal: 0,
      valAxisMaxVal: 10,
      valAxisMajorUnit: 5,
      catAxisOrientation: "maxMin",
    });
  });
  test("a legend, a title and the slide's typeface come across; zeros stay zeros", () => {
    const spec = chartJsToPptx(
      chart({
        type: "bar",
        datasets: [{ label: "S", data: [0, 5, null], backgroundColor: "#1e2bfa" }],
        options: {
          plugins: { legend: { position: "right" }, title: { display: true, text: "Hello" } },
        },
      }),
      fakeCanvas,
    );
    expect(spec.options).toMatchObject({
      showLegend: true,
      legendPos: "r",
      showTitle: true,
      title: "Hello",
      legendFontFace: "Inter",
    });
    expect(spec.types[0].data[0].values).toEqual([0, 5, null]);
  });
  test("a line chart with a filled line is an area chart; a bar and a line on two axes is a combo", () => {
    const area = chartJsToPptx(
      chart({
        type: "line",
        datasets: [
          {
            label: "a",
            data: [1, 2, 3],
            borderColor: "#111111",
            backgroundColor: "#11111133",
            fill: true,
          },
        ],
      }),
      fakeCanvas,
    );
    expect(area.types.map((t: { type: string }) => t.type)).toEqual(["area"]);
    const combo = chartJsToPptx(
      chart({
        type: "bar",
        datasets: [
          { type: "bar", label: "a", data: [1, 2, 3], backgroundColor: "#111111", yAxisID: "y" },
          { type: "line", label: "b", data: [1, 2, 3], borderColor: "#222222", yAxisID: "y1" },
        ],
      }),
      fakeCanvas,
    );
    expect(combo.types.map((t: { type: string }) => t.type)).toEqual(["bar", "line"]);
    expect(combo.types[1].options.secondaryValAxis).toBe(true);
    expect(combo.options.valAxes).toHaveLength(2);
  });
});
