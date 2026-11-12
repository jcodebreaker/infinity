import { Trans } from "@lingui/react/macro";
import type { Artifact } from "@superchat/contracts";
import { isAttachmentImageMimeType } from "@superchat/contracts";
import { PdfViewer } from "../../components/PdfViewer";
import { SandboxedHtmlViewer } from "../../components/SandboxedHtmlViewer";
import { useObjectUrl } from "../../lib/use-object-url";
import { useArtifactExtensions } from "./registry";

/** A file's preview by type: an extension's viewer when one claims it, else the built-in one. */
export function ArtifactPreview({
  artifact,
  bytes,
  onEdited,
  page,
}: {
  artifact: Artifact;
  bytes: Uint8Array;
  /** Open a PDF at this page. */
  page?: number;
  /** A new version was saved from the viewer. */
  onEdited?: (artifactId: string) => void;
}) {
  const extensions = useArtifactExtensions();
  const fallback = <ArtifactPreviewBody artifact={artifact} bytes={bytes} page={page} />;
  for (const extension of extensions) {
    const view = extension.view?.({ artifact, bytes, onEdited, fallback });
    if (view) return view;
  }
  return fallback;
}

function ArtifactPreviewBody({
  artifact,
  bytes,
  page,
}: {
  artifact: Artifact;
  bytes: Uint8Array;
  page?: number;
}) {
  if (artifact.mimeType === "text/html") {
    const html = new TextDecoder("utf-8").decode(bytes);
    return <SandboxedHtmlViewer html={html} title={artifact.name} />;
  }
  if (["text/plain", "text/csv", "application/json"].includes(artifact.mimeType)) {
    const text = new TextDecoder("utf-8").decode(bytes);
    return (
      <pre className="h-full overflow-auto whitespace-pre-wrap break-words bg-background p-6 font-mono text-[13px] leading-6 text-foreground">
        {text}
      </pre>
    );
  }
  if (artifact.mimeType === "application/pdf") {
    return <PdfViewer bytes={bytes} title={artifact.name} page={page} />;
  }
  if (isAttachmentImageMimeType(artifact.mimeType)) {
    return <ImagePreview bytes={bytes} mimeType={artifact.mimeType} name={artifact.name} />;
  }
  return (
    <div className="grid h-full place-items-center px-6 text-center text-sm text-muted-foreground/80">
      <Trans>Preview isn't available for this file type — download it to view it.</Trans>
    </div>
  );
}

function ImagePreview({
  bytes,
  mimeType,
  name,
}: {
  bytes: Uint8Array;
  mimeType: string;
  name: string;
}) {
  const url = useObjectUrl(bytes, mimeType);
  if (!url) return null;
  return (
    <div className="grid h-full place-items-center overflow-auto bg-muted/40 p-4">
      <img src={url} alt={name} className="max-h-full max-w-full rounded-lg shadow-sm" />
    </div>
  );
}
