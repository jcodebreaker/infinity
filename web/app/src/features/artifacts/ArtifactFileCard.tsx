import { i18n } from "@lingui/core";
import { t } from "@lingui/core/macro";
import { Trans, useLingui } from "@lingui/react/macro";
import { ChatMarkdown } from "@superchat/chat-ui/web";
import { isAttachmentImageMimeType } from "@superchat/contracts";
import {
  Button,
  buttonVariants,
  cn,
  Dialog,
  DialogClose,
  DialogContent,
  DialogTitle,
} from "@superchat/ui-web";
import { Download, ExternalLink, X } from "lucide-react";
import type { RefObject } from "react";
import { useEffect, useRef, useState } from "react";
import { MEDIA_ACTION, MediaArt, MediaFrame, MediaTile } from "../../components/MediaTile";
import { PdfViewer } from "../../components/PdfViewer";
import { SandboxedHtmlViewer } from "../../components/SandboxedHtmlViewer";
import { artifactKind, kindLabel } from "../../lib/artifact-kind";
import {
  downloadArtifact,
  downloadArtifactBytes,
  fetchArtifactBytes,
  opensInNewTab,
} from "../../lib/artifact-open";
import { useObjectUrl } from "../../lib/use-object-url";
import {
  ArtifactPreviewThumbnail,
  hasLivePreview,
  useCustomThumbnail,
} from "./ArtifactPreviewThumbnail";

type ArtifactFileCardProps = {
  artifactId: string;
  name: string;
  mimeType: string;
  size: number;
};

const PREVIEWABLE_MIME_TYPES = new Set(["text/markdown", "text/html", "application/pdf"]);
const TEXT_MIME_TYPES = new Set(["text/markdown", "text/html"]);
const EMPTY_BYTES = new Uint8Array(0);
/** A file card's small action (Open, Download): one hairline, ink-2, a little brighter on hover. */
const _FILE_ACTION =
  "inline-flex h-7 items-center gap-1.5 rounded-lg border border-line px-2.5 text-[12.5px] font-medium text-ink-2 transition-colors hover:bg-selection hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring";

export function ArtifactFileCard(props: ArtifactFileCardProps) {
  const { t } = useLingui();
  const previewable = PREVIEWABLE_MIME_TYPES.has(props.mimeType);
  const previewButton = useRef<HTMLButtonElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  // The card's big title: the document's own heading once its preview has loaded.
  const [heading, setHeading] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const kind = artifactKind(props.mimeType, props.name);
  const artifact = {
    id: props.artifactId,
    mimeType: props.mimeType,
    size: props.size,
    name: props.name,
  };
  const customThumbnail = useCustomThumbnail(artifact);

  async function startDownload() {
    setDownloadError(null);
    try {
      await downloadArtifact(props.artifactId, props.name, props.mimeType);
    } catch {
      setDownloadError(t`Could not download ${props.name}. Try again.`);
    }
  }

  // Every file in the Conversation is the same card: a thumbnail on the left, the name and
  // a quiet meta line, then its actions in one row. Kinds the dialog can't show download.
  const openable = previewable || isAttachmentImageMimeType(props.mimeType);
  const open = () => (openable ? setPreviewOpen(true) : void startDownload());
  return (
    <>
      <MediaTile
        openRef={previewButton}
        kind={kind}
        brand={kind === "document" ? t`Report` : kindLabel(kind)}
        title={heading ?? readableFileName(props.name)}
        art={
          hasLivePreview(artifact) || customThumbnail ? (
            <MediaFrame>
              <ArtifactPreviewThumbnail artifact={artifact} onHeading={setHeading} />
            </MediaFrame>
          ) : (
            <MediaArt kind={kind} />
          )
        }
        meta={
          <>
            <b className="font-semibold">{kindLabel(kind)}</b> • {formatBytes(props.size)}
          </>
        }
        openLabel={openable ? t`Open` : t`Download`}
        openAriaLabel={openable ? t`Open ${props.name}` : t`Download ${props.name}`}
        onOpen={open}
        actions={
          openable ? (
            <button
              type="button"
              aria-label={t`Download ${props.name}`}
              title={t`Download`}
              onClick={() => void startDownload()}
              className={MEDIA_ACTION}
            >
              <Download strokeWidth={2} aria-hidden="true" />
            </button>
          ) : undefined
        }
      />
      {downloadError ? <DownloadError message={downloadError} /> : null}
      <Dialog open={previewOpen} onOpenChange={setPreviewOpen}>
        <DialogContent
          showCloseButton={false}
          initialFocus={closeButton}
          finalFocus={previewButton}
          className="flex h-[85vh] w-[90vw] flex-col gap-0 overflow-hidden rounded-2xl p-0 sm:max-w-none"
        >
          <FilePreview {...props} closeButtonRef={closeButton} />
        </DialogContent>
      </Dialog>
    </>
  );
}

function FilePreview({
  artifactId,
  name,
  mimeType,
  closeButtonRef,
}: ArtifactFileCardProps & { closeButtonRef: RefObject<HTMLButtonElement | null> }) {
  const { t } = useLingui();
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [state, setState] = useState<
    | { status: "loading" }
    | { status: "ready"; bytes: Uint8Array; text?: string }
    | { status: "error"; message: string }
  >({ status: "loading" });
  // An object URL so the dialog can offer "Open in new tab" once the bytes are in.
  const objectUrl = useObjectUrl(state.status === "ready" ? state.bytes : EMPTY_BYTES, mimeType);

  useEffect(() => {
    let cancelled = false;
    void fetchArtifactBytes(artifactId)
      .then((bytes) => {
        if (cancelled) return;
        if (!TEXT_MIME_TYPES.has(mimeType)) {
          setState({ status: "ready", bytes });
          return;
        }
        try {
          const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
          setState({ status: "ready", bytes, text });
        } catch {
          setState({ status: "error", message: t`This file is not valid UTF-8 text.` });
        }
      })
      .catch((error) => {
        if (cancelled) return;
        setState({
          status: "error",
          message: error instanceof Error ? error.message : t`Could not load this file.`,
        });
      });
    return () => {
      cancelled = true;
    };
  }, [artifactId, mimeType, t]);

  return (
    <>
      <header className="flex h-14 shrink-0 items-center gap-1 border-b border-border px-5">
        <DialogTitle className="min-w-0 flex-1 truncate text-[14px] leading-5 font-medium text-foreground">
          {name}
        </DialogTitle>
        {state.status === "ready" && objectUrl && opensInNewTab(mimeType) ? (
          <a
            href={objectUrl}
            target="_blank"
            rel="noreferrer noopener"
            className={cn(
              buttonVariants({ variant: "ghost", size: "icon-lg" }),
              "rounded-full text-muted-foreground",
            )}
            aria-label={t`Open ${name} in new tab`}
            title={t`Open in new tab`}
          >
            <ExternalLink />
          </a>
        ) : null}
        <Button
          variant="ghost"
          size="icon-lg"
          className="rounded-full text-muted-foreground"
          aria-label={t`Download ${name}`}
          title={t`Download ${name}`}
          onClick={() =>
            void (async () => {
              setDownloadError(null);
              try {
                if (state.status === "ready") downloadArtifactBytes(name, mimeType, state.bytes);
                else await downloadArtifact(artifactId, name, mimeType);
              } catch {
                setDownloadError(t`Could not download ${name}. Try again.`);
              }
            })()
          }
        >
          <Download />
        </Button>
        <DialogClose
          ref={closeButtonRef}
          aria-label={t`Close preview`}
          render={
            <Button variant="ghost" size="icon-lg" className="rounded-full text-muted-foreground" />
          }
        >
          <X />
        </DialogClose>
      </header>
      {downloadError ? (
        <div className="shrink-0 px-5 pt-4">
          <DownloadError message={downloadError} />
        </div>
      ) : null}
      <div className="relative min-h-0 flex-1 overflow-y-auto">
        {state.status === "loading" ? (
          <div className="p-8 text-muted-foreground">
            <Trans>Loading preview…</Trans>
          </div>
        ) : state.status === "error" ? (
          <div className="m-5 rounded-xl border border-destructive/40 bg-destructive/10 px-4 py-3 text-destructive">
            {state.message}
          </div>
        ) : mimeType === "text/html" && state.text !== undefined ? (
          <SandboxedHtmlViewer html={state.text} title={name} />
        ) : mimeType === "application/pdf" ? (
          <PdfViewer bytes={state.bytes} title={name} />
        ) : isAttachmentImageMimeType(mimeType) ? (
          <ImageFilePreview bytes={state.bytes} mimeType={mimeType} name={name} />
        ) : state.text !== undefined ? (
          <article className="mx-auto w-full max-w-[760px] px-8 py-10 text-[16px] leading-7 text-foreground sm:px-12 sm:py-12">
            <ChatMarkdown>{state.text}</ChatMarkdown>
          </article>
        ) : null}
      </div>
    </>
  );
}

function ImageFilePreview({
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

function DownloadError({ message }: { message: string }) {
  return (
    <div role="alert" className="mt-2 text-left text-[13px] text-destructive">
      {message}
    </div>
  );
}

function formatBytes(size: number) {
  const locale = i18n.locale || "en";
  if (size < 1024) return t`${size} B`;
  const format = (value: number) =>
    new Intl.NumberFormat(locale, {
      minimumFractionDigits: 1,
      maximumFractionDigits: 1,
    }).format(value);
  if (size < 1024 * 1024) return t`${format(size / 1024)} KB`;
  return t`${format(size / (1024 * 1024))} MB`;
}

/** A file name as a title: no extension, separators as spaces, first letter capitalized
 * (`plateau_cafes_montreal.md` → `Plateau cafes montreal`). */
export function readableFileName(name: string): string {
  const base = name
    // A compound kind marker (`q3.deck.html`, `sales.chart.json`) goes with the extension.
    .replace(/(\.(deck|chart|dashboard))?\.[A-Za-z0-9]{1,5}$/i, "")
    .replace(/[_-]+/g, " ")
    .trim();
  return base ? base.charAt(0).toUpperCase() + base.slice(1) : name;
}
