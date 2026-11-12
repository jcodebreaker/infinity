import { isDashboardPageName } from "@superchat/contracts";
import type { ReactNode, RefObject } from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import { SandboxedHtmlViewer } from "../../components/SandboxedHtmlViewer";
import { artifactKind, KIND_ICON } from "../../lib/artifact-kind";
import { decodeArtifactBase64 } from "../../lib/artifact-open";
import { rpc } from "../../lib/rpc";
import { useObjectUrl } from "../../lib/use-object-url";
import { useArtifactExtensions } from "./registry";

// A real preview is only cheap for images and small pages, so cap both the mime
// types and the byte size that get one; everything else keeps its type icon.
const HTML_PREVIEW_MAX_BYTES = 200_000;
/** A dashboard page carries Chart.js inline (about 200 KB) on top of its data. */
const DASHBOARD_PREVIEW_MAX_BYTES = 1_500_000;
const IMAGE_PREVIEW_MAX_BYTES = 3_000_000;

// A realistic desktop viewport, scaled down to cover the (16:10) thumbnail frame
// edge to edge — a page centered in a tiny letterboxed square just reads as broken.
const HTML_VIEWPORT_WIDTH = 1280;
const HTML_VIEWPORT_HEIGHT = 800;

const bytesCache = new Map<string, Uint8Array>();

/** The minimum an artifact card needs to render (and lazily fetch) a preview thumbnail. */
export type PreviewableArtifact = {
  id: string;
  mimeType: string;
  size: number;
  name: string;
  /** Included when known, so an edited artifact's cached bytes don't stick around under the same id. */
  version?: number;
};

/** Whether this artifact gets a real rendered thumbnail (a small image or page), rather than
 * its type icon. */
export function hasLivePreview(
  artifact: Pick<PreviewableArtifact, "mimeType" | "size"> & { name?: string },
): boolean {
  const kind = artifactKind(artifact.mimeType);
  const pageLimit =
    artifact.name && isDashboardPageName(artifact.name)
      ? DASHBOARD_PREVIEW_MAX_BYTES
      : HTML_PREVIEW_MAX_BYTES;
  return (
    (kind === "image" && artifact.size <= IMAGE_PREVIEW_MAX_BYTES) ||
    (kind === "page" && artifact.size <= pageLimit)
  );
}

/** The thumbnail an extension draws for this artifact (a chart, a deck), or null. */
export function useCustomThumbnail(
  artifact: PreviewableArtifact,
  onMeta?: (meta: string) => void,
): ReactNode | null {
  const extensions = useArtifactExtensions();
  for (const extension of extensions) {
    const node = extension.thumbnail?.({
      id: artifact.id,
      name: artifact.name,
      mimeType: artifact.mimeType,
      version: artifact.version,
      onMeta,
    });
    if (node) return node;
  }
  return null;
}

function cacheKey(artifact: PreviewableArtifact): string {
  return artifact.version !== undefined ? `${artifact.id}:${artifact.version}` : artifact.id;
}

/**
 * True once the element has scrolled near the viewport. Stays false forever where
 * `IntersectionObserver` isn't available (some test environments), which just means
 * that thumbnail keeps showing its type icon instead of guessing at visibility.
 */
function useNearViewport(): [RefObject<HTMLDivElement | null>, boolean] {
  const ref = useRef<HTMLDivElement | null>(null);
  const [near, setNear] = useState(false);

  useEffect(() => {
    const node = ref.current;
    if (!node || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setNear(true);
          observer.disconnect();
        }
      },
      { rootMargin: "200px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  return [ref, near];
}

/**
 * Tracks an element's rendered width so a fixed-size viewport can be scaled to cover
 * it exactly. Falls back to `1` (no scale-down) where `ResizeObserver` isn't available.
 */
function useCoverScale(viewportWidth: number): [RefObject<HTMLDivElement | null>, number, boolean] {
  const ref = useRef<HTMLDivElement | null>(null);
  const [scale, setScale] = useState<number | null>(null);

  useEffect(() => {
    const node = ref.current;
    if (!node || typeof ResizeObserver === "undefined") {
      setScale(1);
      return;
    }
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width;
      if (width) setScale(width / viewportWidth);
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [viewportWidth]);

  return [ref, scale ?? 1, scale !== null];
}

/**
 * The one artifact preview thumbnail, shared by the Library grid and the Conversation
 * transcript's file cards. Fetches real content lazily — only once the thumbnail is
 * about to scroll into view, and only for images and small HTML pages — so a long list
 * or transcript never pulls every artifact's full bytes at once. PDFs, decks, larger
 * pages, and anything still off-screen show their type icon instead.
 */
/** A text document's own title: its first Markdown `#` heading, else an HTML `<h1>` or
 * `<title>`. `null` when it has none. */
export function documentHeading(text: string): string | null {
  const markdown = /^#[ \t]+(.+?)[ \t#]*$/m.exec(text)?.[1];
  const html = (/<h1[^>]*>([\s\S]*?)<\/h1>/i.exec(text) ??
    /<title[^>]*>([\s\S]*?)<\/title>/i.exec(text))?.[1];
  const heading = (markdown ?? html?.replace(/<[^>]+>/g, ""))?.replace(/\s+/g, " ").trim();
  return heading ? heading.slice(0, 120) : null;
}

export function ArtifactPreviewThumbnail({
  artifact,
  onReady,
  onHeading,
  onMeta,
  fallback,
}: {
  artifact: PreviewableArtifact;
  /** Drawn instead of the type icon while there is no real preview. */
  fallback?: ReactNode;
  /** Fires once the real preview has content to show, so a card can fade its skeleton out. */
  onReady?: () => void;
  /** The document's own title, once its bytes are here (see {@link documentHeading}). */
  onHeading?: (heading: string) => void;
  /** A short fact read from the file (a deck's "4 slides"), for the card's meta line. */
  onMeta?: (meta: string) => void;
}) {
  const kind = artifactKind(artifact.mimeType);
  const [ref, near] = useNearViewport();
  const eligible = hasLivePreview(artifact);
  const [bytes, setBytes] = useState<Uint8Array | null>(
    () => bytesCache.get(cacheKey(artifact)) ?? null,
  );

  useEffect(() => {
    // Nothing eligible ever gets real bytes, so its type icon is already the whole
    // story — tell the card right away instead of leaving its skeleton stuck on.
    if (!eligible) onReady?.();
  }, [eligible, onReady]);

  useEffect(() => {
    if (!eligible || !near || bytes) return;
    let cancelled = false;
    void rpc.artifacts
      .getById({ artifactId: artifact.id })
      .then((full) => {
        if (cancelled) return;
        const decoded = decodeArtifactBase64(full.contentBase64);
        bytesCache.set(cacheKey(artifact), decoded);
        setBytes(decoded);
      })
      .catch(() => {
        // Best-effort thumbnail; the type icon stays as the fallback, but the card
        // still needs to know there's nothing more to wait for.
        if (!cancelled) onReady?.();
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [eligible, near, bytes, artifact.id]);

  useEffect(() => {
    if (!bytes || !onHeading || kind === "image") return;
    const heading = documentHeading(new TextDecoder("utf-8").decode(bytes));
    if (heading) onHeading(heading);
  }, [bytes, kind, onHeading]);

  const Icon = KIND_ICON[kind];
  const custom = useCustomThumbnail(artifact, onMeta);

  return (
    <div ref={ref} className="grid h-full w-full place-items-center">
      {custom ? (
        custom
      ) : bytes && kind === "image" ? (
        <ImageThumbnail bytes={bytes} mimeType={artifact.mimeType} onReady={onReady} />
      ) : bytes && kind === "page" ? (
        <ScaledHtmlThumbnail bytes={bytes} title={artifact.name} onReady={onReady} />
      ) : (
        (fallback ?? <Icon size={36} strokeWidth={1.5} className="text-muted-foreground/50" />)
      )}
    </div>
  );
}

function ImageThumbnail({
  bytes,
  mimeType,
  onReady,
}: {
  bytes: Uint8Array;
  mimeType: string;
  onReady?: () => void;
}) {
  const url = useObjectUrl(bytes, mimeType);
  if (!url) return null;
  return <img src={url} alt="" className="h-full w-full object-cover" onLoad={onReady} />;
}

function ScaledHtmlThumbnail({
  bytes,
  title,
  onReady,
}: {
  bytes: Uint8Array;
  title: string;
  onReady?: () => void;
}) {
  const html = useMemo(() => new TextDecoder("utf-8").decode(bytes), [bytes]);
  const [ref, scale, measured] = useCoverScale(HTML_VIEWPORT_WIDTH);

  useEffect(() => {
    if (measured) onReady?.();
  }, [measured, onReady]);

  return (
    <div ref={ref} className="pointer-events-none h-full w-full overflow-hidden">
      <div
        className="origin-top-left"
        style={{
          width: HTML_VIEWPORT_WIDTH,
          height: HTML_VIEWPORT_HEIGHT,
          transform: `scale(${scale})`,
        }}
      >
        <SandboxedHtmlViewer html={html} title={title} />
      </div>
    </div>
  );
}
