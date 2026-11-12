import { useLingui } from "@lingui/react/macro";
import { ChatMarkdown } from "@superchat/chat-ui/web";
import {
  deckSlideCount,
  isAttachmentImageMimeType,
  isDeckArtifactName,
} from "@superchat/contracts";
import { cn } from "@superchat/ui-web";
import { File, FileText, Presentation, Table2 } from "lucide-react";
import { type RefObject, useEffect, useRef, useState } from "react";
import { formatSize } from "../../../components/cards/catalog";
import { SandboxedHtmlViewer } from "../../../components/SandboxedHtmlViewer";
import { decodeArtifactBase64 } from "../../../lib/artifact-open";
import { rpc } from "../../../lib/rpc";
import { useObjectUrl } from "../../../lib/use-object-url";
import { useVisiblePoll } from "../../../lib/use-visible-poll";
import { documentHeading } from "../ArtifactPreviewThumbnail";
import { useArtifactExtensions } from "../registry";

const bytesCache = new Map<string, { mimeType: string; bytes: Uint8Array }>();
const HTML_VIEWPORT_WIDTH = 1280;
const MARKDOWN_MAX_LINES = 40;
const CSV_MAX_ROWS = 8;
const CSV_MAX_COLUMNS = 6;
/** Larger deliverables keep the cover: decoding them for a thumbnail is not worth it. */
const PREVIEW_MAX_BYTES = 3_000_000;
/** How often a card whose file is still being saved re-checks its version list. */
const PENDING_POLL_MS = 3000;

/** Bytes with nothing to show yet: an empty file, or a deck with no slide so far. */
function isPendingContent(name: string, mimeType: string, bytes: Uint8Array): boolean {
  if (mimeType.startsWith("image/") || mimeType === "application/pdf") return bytes.length === 0;
  if (!mimeType.startsWith("text/")) return false;
  const text = new TextDecoder("utf-8").decode(bytes);
  if (!text.trim()) return true;
  return isDeckArtifactName(name) && deckSlideCount(text) === 0;
}

type Loaded = { mimeType: string; bytes: Uint8Array };
type State = { status: "idle" | "loading" | "pending" | "failed" } | ({ status: "ready" } & Loaded);

/** True once the element nears the viewport, so a long chat only fetches what scrolls into view.
 * Without IntersectionObserver (tests) it never fires and the cover stays. */
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
      { rootMargin: "300px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  return [ref, near];
}

/** The card body: a live ~260px preview of the file, or a quiet cover when it can't preview. */
export function ArtifactInlinePreview({
  artifactId,
  name,
  kind,
  size,
  version,
  onFailed,
  onHeading,
  onText,
  className,
}: {
  /** The document's own title, once its bytes are here (see `documentHeading`). */
  onHeading?: (heading: string) => void;
  /** The file's text, once its bytes are here (a deck card counts its slides). */
  onText?: (text: string) => void;
  /** Overrides the box (the result tile frames it smaller). */
  className?: string;
  artifactId: string;
  name: string;
  kind?: string;
  size?: number;
  version?: number;
  onFailed?: () => void;
}) {
  const [ref, near] = useNearViewport();
  const [state, setState] = useState<State>({ status: "idle" });
  // The newest version the engine lists for this file: a card rendered while its file was still
  // being saved follows it, so the preview refetches instead of staying on what it first saw.
  const [latestVersionId, setLatestVersionId] = useState("");
  const [attempt, setAttempt] = useState(0);
  const key = `${artifactId}:${version ?? ""}:${latestVersionId}:${attempt}`;
  // A capability's own preview (a live chart) takes the box; the default fetch below stands down.
  const extensions = useArtifactExtensions();
  const custom = extensions
    .map((extension) => extension.inline?.({ artifactId, name, version, near }) ?? null)
    .find((node) => node !== null);

  // biome-ignore lint/correctness/useExhaustiveDependencies: onFailed is a notification only
  useEffect(() => {
    if (!near || custom) return;
    if (size !== undefined && size > PREVIEW_MAX_BYTES) {
      setState({ status: "failed" });
      return;
    }
    const cached = bytesCache.get(key);
    if (cached) {
      setState({ status: "ready", ...cached });
      return;
    }
    let cancelled = false;
    setState({ status: "loading" });
    rpc.artifacts
      .getById({ artifactId })
      .then((artifact) => {
        if (cancelled) return;
        const loaded = {
          mimeType: artifact.mimeType,
          bytes: decodeArtifactBase64(artifact.contentBase64),
        };
        // Nothing to draw yet: stay on the skeleton and let the poll bring the real content.
        if (isPendingContent(name, loaded.mimeType, loaded.bytes)) {
          setState({ status: "pending" });
          return;
        }
        bytesCache.set(key, loaded);
        setState({ status: "ready", ...loaded });
      })
      .catch(() => {
        if (cancelled) return;
        setState({ status: "failed" });
        onFailed?.();
      });
    return () => {
      cancelled = true;
    };
  }, [near, key, artifactId, size, custom]);

  // While the file is empty, still loading or failed, watch its version list; a newer version
  // changes the key above and the preview fetches again.
  useVisiblePoll(
    async () => {
      const list = await rpc.artifacts.listVersions({ familyId: artifactId });
      const top = list[0];
      if (top) setLatestVersionId(top.id);
      // Same version, still empty (or the first read failed): read it again.
      if (state.status === "pending" || state.status === "failed") setAttempt((n) => n + 1);
    },
    PENDING_POLL_MS,
    near &&
      !custom &&
      state.status !== "ready" &&
      !(size !== undefined && size > PREVIEW_MAX_BYTES),
  );

  useEffect(() => {
    if (state.status !== "ready" || state.mimeType.startsWith("image/")) return;
    if (!onHeading && !onText) return;
    const text = new TextDecoder("utf-8").decode(state.bytes);
    onText?.(text);
    const heading = onHeading ? documentHeading(text) : null;
    if (heading) onHeading?.(heading);
  }, [state, onHeading, onText]);

  const mimeType = state.status === "ready" ? state.mimeType : "";
  return (
    <div
      ref={ref}
      data-testid="artifact-preview"
      data-preview={custom ? "custom" : state.status === "ready" ? previewKind(mimeType) : "cover"}
      className={cn(
        "relative h-[200px] overflow-hidden rounded-lg border border-border bg-muted/40 sm:h-[260px]",
        className,
      )}
    >
      {custom ? (
        custom
      ) : state.status === "ready" ? (
        <PreviewBody name={name} kind={kind} size={size} {...state} />
      ) : (
        <Cover
          name={name}
          kind={kind}
          size={size}
          busy={state.status === "loading" || state.status === "pending"}
        />
      )}
    </div>
  );
}

export function previewKind(
  mimeType: string,
): "html" | "markdown" | "image" | "pdf" | "csv" | "cover" {
  if (mimeType === "text/html") return "html";
  if (mimeType === "text/markdown") return "markdown";
  if (mimeType === "application/pdf") return "pdf";
  if (mimeType === "text/csv") return "csv";
  if (isAttachmentImageMimeType(mimeType)) return "image";
  return "cover";
}

function PreviewBody({
  name,
  kind,
  size,
  mimeType,
  bytes,
}: {
  name: string;
  kind?: string;
  size?: number;
  mimeType: string;
  bytes: Uint8Array;
}) {
  switch (previewKind(mimeType)) {
    case "html":
      return <HtmlPreview html={new TextDecoder().decode(bytes)} title={name} />;
    case "markdown": {
      const text = new TextDecoder()
        .decode(bytes)
        .split("\n")
        .slice(0, MARKDOWN_MAX_LINES)
        .join("\n");
      return (
        <div className="h-full overflow-hidden bg-background px-5 py-4 text-[14px] [mask-image:linear-gradient(to_bottom,black_75%,transparent)]">
          <ChatMarkdown>{text}</ChatMarkdown>
        </div>
      );
    }
    case "image":
      return <ImagePreview bytes={bytes} mimeType={mimeType} name={name} />;
    case "pdf":
      return <PdfPreview bytes={bytes} title={name} />;
    case "csv":
      return <CsvPreview text={new TextDecoder().decode(bytes)} />;
    default:
      return <Cover name={name} kind={kind} size={size} />;
  }
}

/** A desktop-width page scaled to the card's width; the page scrolls inside it. */
function HtmlPreview({ html, title }: { html: string; title: string }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [box, setBox] = useState({ scale: 0.5, height: 520 });
  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const measure = () => {
      const scale = Math.min(1, node.clientWidth / HTML_VIEWPORT_WIDTH) || 0.5;
      setBox({ scale, height: node.clientHeight / scale });
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  return (
    <div ref={ref} className="h-full w-full overflow-hidden bg-white">
      <div
        style={{
          width: HTML_VIEWPORT_WIDTH,
          height: box.height,
          transform: `scale(${box.scale})`,
          transformOrigin: "top left",
        }}
      >
        <SandboxedHtmlViewer html={html} title={title} />
      </div>
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
  return url ? (
    <div className="grid h-full place-items-center p-2">
      <img src={url} alt={name} className="max-h-full max-w-full rounded" />
    </div>
  ) : null;
}

function PdfPreview({ bytes, title }: { bytes: Uint8Array; title: string }) {
  const url = useObjectUrl(bytes, "application/pdf");
  return url ? (
    <iframe
      title={title}
      src={`${url}#page=1&toolbar=0&navpanes=0`}
      className="h-full w-full border-0 bg-white"
    />
  ) : null;
}

function CsvPreview({ text }: { text: string }) {
  const rows = text
    .split(/\r?\n/)
    .filter(Boolean)
    .slice(0, CSV_MAX_ROWS)
    .map((line) => line.split(",").slice(0, CSV_MAX_COLUMNS));
  const [head, ...body] = rows;
  return (
    <div className="h-full overflow-hidden bg-background">
      <table className="w-full text-start text-[12.5px]">
        <thead>
          <tr>
            {head?.map((cell, index) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: static preview rows
              <th key={index} className="truncate border-b border-border px-3 py-1.5 font-medium">
                {cell}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.map((row, rowIndex) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: static preview rows
            <tr key={rowIndex}>
              {row.map((cell, index) => (
                // biome-ignore lint/suspicious/noArrayIndexKey: static preview rows
                <td key={index} className="max-w-[10rem] truncate px-3 py-1 text-muted-foreground">
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Type icon, title and size: what a file shows before (or instead of) a preview. */
function Cover({
  name,
  kind,
  size,
  busy,
}: {
  name: string;
  kind?: string;
  size?: number;
  busy?: boolean;
}) {
  const { t } = useLingui();
  const extension = (kind ?? name.split(".").pop() ?? "").toLowerCase();
  const Icon =
    extension === "pptx"
      ? Presentation
      : extension === "xlsx" || extension === "csv"
        ? Table2
        : extension === "docx" || extension === "pdf" || extension === "md"
          ? FileText
          : File;
  return (
    <div
      className={`grid h-full place-items-center px-6 text-center ${busy ? "animate-pulse" : ""}`}
      aria-busy={busy}
    >
      <div className="flex min-w-0 flex-col items-center gap-2 text-muted-foreground">
        <Icon size={28} strokeWidth={1.4} aria-hidden />
        <span className="max-w-full truncate text-[13.5px] text-foreground" dir="auto">
          {name}
        </span>
        {size !== undefined ? (
          <span className="text-[12px]">{busy ? t`Loading…` : formatSize(size)}</span>
        ) : null}
      </div>
    </div>
  );
}
