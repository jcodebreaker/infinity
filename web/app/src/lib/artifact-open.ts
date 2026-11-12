import { rpc } from "./rpc";

/** Where an artifact card sits: the Muse it belongs to. */
export type ArtifactTarget = { botId: string };

export function decodeArtifactBase64(contentBase64: string): Uint8Array {
  const binary = atob(contentBase64);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

export async function fetchArtifactBytes(artifactId: string): Promise<Uint8Array> {
  const artifact = await rpc.artifacts.getById({ artifactId });
  return decodeArtifactBase64(artifact.contentBase64);
}

export function downloadArtifactBytes(name: string, mimeType: string, bytes: Uint8Array): void {
  const blob = new Blob([new Uint8Array(bytes)], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

export async function downloadArtifact(
  artifactId: string,
  name: string,
  mimeType: string,
): Promise<void> {
  const bytes = await fetchArtifactBytes(artifactId);
  downloadArtifactBytes(name, mimeType, bytes);
}

/**
 * Types a browser shows without running script. A `blob:` URL opened in its own tab runs at the
 * app's origin, so HTML (a page, a deck), SVG and anything else that can script never opens there.
 */
const INERT_IN_A_TAB = new Set([
  "application/pdf",
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "image/avif",
  "text/plain",
]);

export function opensInNewTab(mimeType: string): boolean {
  return INERT_IN_A_TAB.has((mimeType.split(";")[0] ?? "").trim().toLowerCase());
}
