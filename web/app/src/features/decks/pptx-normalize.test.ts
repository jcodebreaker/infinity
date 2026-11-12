import JSZip from "jszip";
import { expect, it } from "vitest";
import {
  isPptxArtifact,
  PPTX_MIME_TYPE,
  plainCategoryRefs,
  pptxForPreview,
} from "./pptx-normalize";

// The category block PptxGenJS writes for a bar chart (whitespace as it writes it).
const MULTI = `<c:cat>  <c:multiLvlStrRef>    <c:f>Sheet1!$A$2:$A$3</c:f>    <c:multiLvlStrCache>      <c:ptCount val="2"/><c:lvl><c:pt idx="0"><c:v>Q1</c:v></c:pt><c:pt idx="1"><c:v>Q2</c:v></c:pt></c:lvl>    </c:multiLvlStrCache>  </c:multiLvlStrRef></c:cat>`;
const PLAIN = `<c:cat>  <c:strRef><c:f>Sheet1!$A$2:$A$3</c:f><c:strCache><c:ptCount val="2"/><c:pt idx="0"><c:v>Q1</c:v></c:pt><c:pt idx="1"><c:v>Q2</c:v></c:pt></c:strCache></c:strRef></c:cat>`;
const TWO_LEVELS = `<c:cat><c:multiLvlStrRef><c:f>Sheet1!$A$2:$B$3</c:f><c:multiLvlStrCache><c:ptCount val="2"/><c:lvl><c:pt idx="0"><c:v>a</c:v></c:pt></c:lvl><c:lvl><c:pt idx="0"><c:v>b</c:v></c:pt></c:lvl></c:multiLvlStrCache></c:multiLvlStrRef></c:cat>`;

async function pptx(parts: Record<string, string>): Promise<Uint8Array> {
  const zip = new JSZip();
  for (const [name, text] of Object.entries(parts)) zip.file(name, text);
  return zip.generateAsync({ type: "uint8array" });
}

it("rewrites a one-level category list to the plain form, keeping every label", () => {
  expect(plainCategoryRefs(MULTI)).toBe(PLAIN);
});

it("leaves multi-level categories and other markup alone", () => {
  expect(plainCategoryRefs(TWO_LEVELS)).toBe(TWO_LEVELS);
  expect(plainCategoryRefs(PLAIN)).toBe(PLAIN);
});

it("rewrites only chart parts of the preview copy", async () => {
  const input = await pptx({
    "ppt/charts/chart1.xml": `<c:chartSpace>${MULTI}</c:chartSpace>`,
    "ppt/slides/slide1.xml": MULTI,
  });
  const out = await JSZip.loadAsync(await pptxForPreview(input));
  expect(await out.file("ppt/charts/chart1.xml")?.async("string")).toBe(
    `<c:chartSpace>${PLAIN}</c:chartSpace>`,
  );
  expect(await out.file("ppt/slides/slide1.xml")?.async("string")).toBe(MULTI);
});

it("returns the same bytes when no chart needs the rewrite", async () => {
  const input = await pptx({ "ppt/charts/chart1.xml": PLAIN, "ppt/slides/slide1.xml": "<p/>" });
  expect(await pptxForPreview(input)).toBe(input);
});

it("recognises PowerPoint files by type or by name", () => {
  expect(isPptxArtifact({ name: "a", mimeType: PPTX_MIME_TYPE })).toBe(true);
  expect(isPptxArtifact({ name: "A.PPTX", mimeType: "application/octet-stream" })).toBe(true);
  expect(isPptxArtifact({ name: "a.deck.html", mimeType: "text/html" })).toBe(false);
  expect(isPptxArtifact({ name: "a.ppt", mimeType: "application/vnd.ms-powerpoint" })).toBe(false);
});
