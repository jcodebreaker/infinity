import JSZip from "jszip";

/** The OOXML main type of a PowerPoint (.pptx) file. */
export const PPTX_MIME_TYPE =
  "application/vnd.openxmlformats-officedocument.presentationml.presentation";

/** True for a PowerPoint file the preview can draw (by mime type, or by name when the type is generic). */
export function isPptxArtifact(artifact: { name: string; mimeType: string }): boolean {
  return artifact.mimeType === PPTX_MIME_TYPE || artifact.name.toLowerCase().endsWith(".pptx");
}

// PptxGenJS (what the Muse's native PowerPoint skill writes) stores a chart's categories as a
// one-level <c:multiLvlStrRef>. PowerPoint reads that as a plain category list; the preview
// renderer only reads <c:strRef>, so the axis labels would be missing. The two forms hold the same
// data, so the preview's private copy is rewritten to the plain form. Charts with more than one
// category level are left alone, and the saved file is never touched.
const ONE_LEVEL_CATEGORIES =
  /<c:multiLvlStrRef>\s*(<c:f>[\s\S]*?<\/c:f>)\s*<c:multiLvlStrCache>\s*(<c:ptCount val="\d+"\/>)\s*<c:lvl>((?:(?!<\/c:lvl>)[\s\S])*)<\/c:lvl>\s*<\/c:multiLvlStrCache>\s*<\/c:multiLvlStrRef>/g;

export function plainCategoryRefs(chartXml: string): string {
  return chartXml.replace(
    ONE_LEVEL_CATEGORIES,
    (_all, formula: string, count: string, points: string) =>
      `<c:strRef>${formula}<c:strCache>${count}${points}</c:strCache></c:strRef>`,
  );
}

/** The bytes the preview renderer reads: the file with its chart categories in the plain form. */
export async function pptxForPreview(bytes: Uint8Array): Promise<Uint8Array> {
  const zip = await JSZip.loadAsync(bytes);
  let changed = false;
  for (const name of Object.keys(zip.files)) {
    if (!/^ppt\/charts\/chart\d+\.xml$/.test(name)) continue;
    const xml = await zip.file(name)?.async("string");
    if (!xml?.includes("<c:multiLvlStrRef>")) continue;
    const next = plainCategoryRefs(xml);
    if (next === xml) continue;
    zip.file(name, next);
    changed = true;
  }
  return changed ? zip.generateAsync({ type: "uint8array", compression: "DEFLATE" }) : bytes;
}
