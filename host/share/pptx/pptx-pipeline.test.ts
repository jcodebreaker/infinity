// Runs the ported pptx-generator pipeline (skill/pptx-generator/SKILL.md: slide modules exporting
// createSlide(pres, theme), compiled by compile.js) against the pinned, patched PptxGenJS the
// host image installs (runtime/install.sh), then reads the .pptx back as a zip.
import { execFileSync } from "node:child_process";
import { cpSync, existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, beforeAll, describe, expect, test } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const runtimeDir = join(here, "runtime");
const nodeModules = join(runtimeDir, "node_modules");
const data = JSON.parse(readFileSync(join(here, "fixtures/slides/data.json"), "utf8")) as {
  revenue: Series;
  trend: Series;
  share: Series;
  table: string[][];
};

interface Series {
  name: string;
  labels: string[];
  values: number[];
}

type Zip = {
  files: Record<string, unknown>;
  file(name: string): { async(type: "string"): Promise<string> } | null;
};

let work = "";
let zip: Zip;
let JSZip: { loadAsync(data: Buffer): Promise<Zip> };

async function text(name: string, from: Zip = zip): Promise<string> {
  const entry = from.file(name);
  if (!entry) throw new Error(`missing part ${name}`);
  return entry.async("string");
}

/** The numbers of `<c:val>` in a chart part, series by series. */
function chartValues(chartXml: string): number[][] {
  return [...chartXml.matchAll(/<c:val>([\s\S]*?)<\/c:val>/g)].map((val) =>
    [...(val[1] ?? "").matchAll(/<c:v>([^<]*)<\/c:v>/g)].map((v) => Number(v[1])),
  );
}

function chartCategories(chartXml: string): string[][] {
  return [...chartXml.matchAll(/<c:cat>([\s\S]*?)<\/c:cat>/g)].map((cat) =>
    [...(cat[1] ?? "").matchAll(/<c:v>([^<]*)<\/c:v>/g)].map((v) => v[1] ?? ""),
  );
}

async function chartParts(): Promise<{ name: string; xml: string }[]> {
  const names = Object.keys(zip.files)
    .filter((name) => /^ppt\/charts\/chart\d+\.xml$/.test(name))
    .sort();
  return Promise.all(names.map(async (name) => ({ name, xml: await text(name) })));
}

/** Reads a chart's embedded workbook (found through the chart part's relationships). */
async function workbookOf(chartName: string): Promise<Zip> {
  const base = chartName.split("/").pop() ?? "";
  const rels = await text(`ppt/charts/_rels/${base}.rels`);
  const target = /Target="\.\.\/embeddings\/([^"]+\.xlsx)"/.exec(rels)?.[1];
  if (!target) throw new Error(`${chartName} has no embedded workbook`);
  const bytes = zip.file(`ppt/embeddings/${target}`);
  if (!bytes) throw new Error(`workbook part ppt/embeddings/${target} is missing`);
  const buffer = Buffer.from(
    await (bytes as unknown as { async(t: "uint8array"): Promise<Uint8Array> }).async("uint8array"),
  );
  return JSZip.loadAsync(buffer);
}

beforeAll(async () => {
  // The same script the Dockerfile runs; a no-op download when node_modules is already there.
  if (!existsSync(join(nodeModules, "pptxgenjs/dist/pptxgen.cjs.js"))) {
    execFileSync("sh", [join(runtimeDir, "install.sh")], { stdio: "inherit", timeout: 180_000 });
  }
  JSZip = createRequire(join(nodeModules, "pptxgenjs/package.json"))("jszip");
  work = mkdtempSync(join(tmpdir(), "superchat-pptx-"));
  cpSync(join(here, "fixtures/slides"), join(work, "slides"), { recursive: true });
  const out = join(work, "deck.pptx");
  execFileSync("node", ["compile.js"], {
    cwd: join(work, "slides"),
    env: { ...process.env, NODE_PATH: nodeModules, SUPERCHAT_PPTX_OUT: out },
    timeout: 60_000,
  });
  zip = await JSZip.loadAsync(readFileSync(out));
}, 240_000);

afterAll(() => {
  if (work) rmSync(work, { recursive: true, force: true });
});

describe("pptx-generator pipeline output", () => {
  test("builds four slides", () => {
    const slides = Object.keys(zip.files).filter((n) => /^ppt\/slides\/slide\d+\.xml$/.test(n));
    expect(slides).toHaveLength(4);
  });

  test("bar, line and pie are native chart parts with the computed values", async () => {
    const charts = await chartParts();
    expect(charts).toHaveLength(3);
    const byKind = (tag: string) => charts.find((c) => c.xml.includes(`<c:${tag}>`));
    const bar = byKind("barChart");
    const line = byKind("lineChart");
    const pie = byKind("pieChart");
    expect(bar && line && pie).toBeTruthy();
    expect(chartValues(bar?.xml ?? "")).toEqual([data.revenue.values]);
    expect(chartCategories(bar?.xml ?? "")).toEqual([data.revenue.labels]);
    expect(chartValues(line?.xml ?? "")).toEqual([data.trend.values]);
    expect(chartCategories(line?.xml ?? "")).toEqual([data.trend.labels]);
    expect(chartValues(pie?.xml ?? "")).toEqual([data.share.values]);
    expect(chartCategories(pie?.xml ?? "")).toEqual([data.share.labels]);
  });

  test("each chart is a graphicFrame bound to its chart part, never a picture", async () => {
    const chartSlides: string[] = [];
    for (const name of Object.keys(zip.files).filter((n) =>
      /^ppt\/slides\/slide\d+\.xml$/.test(n),
    )) {
      const xml = await text(name);
      if (xml.includes("<c:chart ")) chartSlides.push(xml);
    }
    expect(chartSlides).toHaveLength(2);
    const frames = chartSlides.reduce((n, xml) => n + (xml.match(/<c:chart /g) ?? []).length, 0);
    expect(frames).toBe(3);
    for (const xml of chartSlides) {
      expect(xml).not.toContain("<p:pic>");
      expect(xml).toContain("drawingml/2006/chart");
    }
    const types = await text("[Content_Types].xml");
    expect(types).toContain("drawingml.chart+xml");
  });

  test("every chart embeds a workbook with the series (Edit Data)", async () => {
    const expected: Record<string, Series> = {
      barChart: data.revenue,
      lineChart: data.trend,
      pieChart: data.share,
    };
    for (const { name, xml } of await chartParts()) {
      const kind = Object.keys(expected).find((tag) => xml.includes(`<c:${tag}>`)) ?? "";
      const series = expected[kind];
      expect(series, name).toBeDefined();
      const workbook = await workbookOf(name);
      const sheet = await text("xl/worksheets/sheet1.xml", workbook);
      const strings = [
        ...(await text("xl/sharedStrings.xml", workbook)).matchAll(/<t[^>]*>([^<]*)<\/t>/g),
      ].map((m) => m[1]);
      // Every label and the series name is stored in the workbook's shared strings.
      for (const label of series?.labels ?? []) expect(strings).toContain(label);
      expect(strings).toContain(series?.name);
      // The numeric cells read back, a real zero included (PptxGenJS wrote it as an empty cell).
      expect(sheet).not.toContain("<v></v>");
      const numbers = [...sheet.matchAll(/<c [^>]*><v>(-?[\d.]+)<\/v><\/c>/g)].map((m) =>
        Number(m[1]),
      );
      for (const value of series?.values ?? []) expect(numbers, name).toContain(value);
    }
  });

  test("workbook table refs are valid ranges (PptxGenJS #1531 / #1537 applied)", async () => {
    for (const { name } of await chartParts()) {
      const workbook = await workbookOf(name);
      const table = await text("xl/tables/table1.xml", workbook);
      const ref = /<table [^>]*\bref="([^"]*)"/.exec(table)?.[1] ?? "";
      expect(ref, name).toMatch(/^A1:[A-Z]+\d+$/);
      expect(ref).not.toContain("'");
      const dimension = /<dimension ref="([^"]*)"/.exec(
        await text("xl/worksheets/sheet1.xml", workbook),
      )?.[1];
      expect(ref).toBe(dimension);
    }
  });

  test("2D charts reference only axes they define (PptxGenJS #1540 applied)", async () => {
    for (const { name, xml } of await chartParts()) {
      if (xml.includes("<c:pieChart>")) continue; // a pie has no axes
      const defined = [
        ...xml.matchAll(/<c:(?:catAx|valAx|serAx|dateAx)>\s*<c:axId val="(\d+)"/g),
      ].map((m) => m[1]);
      const plot =
        /<c:(?:barChart|lineChart)>([\s\S]*?)<\/c:(?:barChart|lineChart)>/.exec(xml)?.[1] ?? "";
      const used = [...plot.matchAll(/<c:axId val="(\d+)"\/>/g)].map((m) => m[1]);
      expect(used, name).toHaveLength(2);
      for (const id of used) expect(defined, name).toContain(id);
    }
  });

  test("the table is a native a:tbl with the computed cells", async () => {
    let tableXml = "";
    for (const name of Object.keys(zip.files).filter((n) =>
      /^ppt\/slides\/slide\d+\.xml$/.test(n),
    )) {
      const xml = await text(name);
      if (xml.includes("<a:tbl>")) tableXml = xml;
    }
    expect(tableXml).not.toBe("");
    expect(tableXml).not.toContain("<p:pic>");
    const rows = [...tableXml.matchAll(/<a:tr [^>]*>([\s\S]*?)<\/a:tr>/g)].map((tr) =>
      [...(tr[1] ?? "").matchAll(/<a:tc[ >][\s\S]*?<\/a:tc>/g)].map((tc) =>
        [...tc[0].matchAll(/<a:t>([^<]*)<\/a:t>/g)].map((t) => t[1]).join(""),
      ),
    );
    expect(rows).toEqual(data.table);
    expect(tableXml.match(/<a:gridCol /g)).toHaveLength(3);
  });

  test("no slide carries a picture standing in for content", async () => {
    for (const name of Object.keys(zip.files).filter((n) =>
      /^ppt\/slides\/slide\d+\.xml$/.test(n),
    )) {
      expect(await text(name), name).not.toContain("<p:pic>");
    }
  });
});
