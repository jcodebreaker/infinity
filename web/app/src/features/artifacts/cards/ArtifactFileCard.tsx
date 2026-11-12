import { plural } from "@lingui/core/macro";
import { useLingui } from "@lingui/react/macro";
import { deckSlideCount, isDeckArtifactName, type ReplyCardDataOf } from "@superchat/contracts";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@superchat/ui-web";
import { Download, ExternalLink, MoreHorizontal, Palette } from "lucide-react";
import { lazy, Suspense, useCallback, useState } from "react";
import { catalog, formatSize } from "../../../components/cards/catalog";
import {
  useFileStudio,
  useLatestSavedFile,
  useReplyCardBotId,
} from "../../../components/cards/context";
import { MEDIA_ACTION, MediaFrame, MediaTile } from "../../../components/MediaTile";
import { type ArtifactKind, kindLabel } from "../../../lib/artifact-kind";
import {
  decodeArtifactBase64,
  downloadArtifactBytes,
  opensInNewTab,
} from "../../../lib/artifact-open";
import { rpc } from "../../../lib/rpc";
import { setDeckThemeOpen } from "../../decks/deck-ui-state";
import { readableFileName } from "../ArtifactFileCard";
import { artifactKey } from "../editor/editor-state";
import { useArtifactExtensions } from "../registry";
import { ArtifactInlinePreview } from "./ArtifactInlinePreview";

// The expanded view pulls in the PDF and markdown viewers; load them only when opened.
const ArtifactPreviewDialog = lazy(() =>
  import("../library/ArtifactPreviewDialog").then((m) => ({
    default: m.ArtifactPreviewDialog,
  })),
);

/**
 * A file the Muse saved with `artifact_save`, shown as a card with a live preview in the
 * conversation. Expand opens the Library's large viewer; Download and Open in new tab sit in the
 * menu. The same deliverable the Library lists, addressed by artifact id; a card without one
 * (an external `url` file) falls back to the plain file card.
 */
export function ArtifactFileCard({
  title,
  data,
  updating = false,
}: {
  title?: string;
  data: ReplyCardDataOf<"file">;
  /** A Helper is still writing it (the work strip's draft): marked "Updating". */
  updating?: boolean;
}) {
  // A capability may draw the result itself (a chart is a chart, never a JSON file card).
  const extensions = useArtifactExtensions();
  const artifactId = data.artifactId;
  const custom = artifactId
    ? extensions
        .map((extension) =>
          extension.result?.({ artifactId, name: data.name, version: data.version, data }),
        )
        .find((node) => node != null)
    : null;
  if (custom) return <>{custom}</>;
  return data.artifactId ? (
    <SavedFileCard title={title} data={data} updating={updating} />
  ) : (
    <catalog.file title={title} data={data} />
  );
}

function SavedFileCard({
  title,
  data,
  updating,
}: {
  title?: string;
  data: ReplyCardDataOf<"file">;
  updating: boolean;
}) {
  const { t } = useLingui();
  const artifactId = data.artifactId;
  const [open, setOpen] = useState(false);
  const [failed, setFailed] = useState(false);
  // Without a title from Superchat, the card reads the document's own heading once it loads.
  const [documentTitle, setDocumentTitle] = useState<string | null>(null);
  const isDeck = isDeckArtifactName(data.name);
  // A deck reads as a deck: "Deck · 7 slides" once its file is here.
  const [slides, setSlides] = useState<number | null>(null);
  const countSlides = useCallback((text: string) => setSlides(deckSlideCount(text) || null), []);
  const studio = useFileStudio();
  const botId = useReplyCardBotId();
  const newest = useLatestSavedFile(data.name);
  if (!artifactId) return null;
  const version = data.version ?? 1;
  const versions = Math.max(data.versions ?? 1, version);
  const superseded = newest !== undefined && newest.version > version ? newest : null;
  const heading = title || documentTitle || readableFileName(data.name);

  function expand() {
    if (!artifactId) return;
    if (studio) studio.open(artifactId, title);
    else setOpen(true);
  }
  // A deck's look was chosen for it (ADR 0025): the studio opens on the Theme gallery to change it.
  const changeLook =
    isDeck && studio && botId
      ? () => {
          setDeckThemeOpen(artifactKey({ botId, name: data.name }), true);
          studio.open(artifactId, title);
        }
      : null;
  const tileKind = isDeck ? "deck" : data.kind ? kindFromLabel(data.kind) : "file";
  const kindText = isDeck ? kindLabel("deck") : data.kind?.toUpperCase();
  // A page, a deck or an SVG would run at the app's origin in a tab; those open in the editor.
  const canOpenInNewTab =
    tileKind !== "page" && tileKind !== "deck" && data.kind?.toLowerCase() !== "svg";
  const meta = [
    kindText,
    isDeck && slides
      ? plural(slides, { one: "# slide", other: "# slides" })
      : data.size !== undefined
        ? formatSize(data.size)
        : null,
    data.byYou ? t`Exported by you` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  async function fetchFile() {
    if (!artifactId) return null;
    setFailed(false);
    try {
      const artifact = await rpc.artifacts.getById({ artifactId });
      return { artifact, bytes: decodeArtifactBase64(artifact.contentBase64) };
    } catch {
      setFailed(true);
      return null;
    }
  }

  async function download() {
    const file = await fetchFile();
    if (file) downloadArtifactBytes(file.artifact.name, file.artifact.mimeType, file.bytes);
  }

  async function openInNewTab() {
    // Opened synchronously so the popup isn't blocked; pointed at the bytes once they arrive.
    const tab = window.open("", "_blank");
    const file = await fetchFile();
    if (!file || !opensInNewTab(file.artifact.mimeType)) {
      tab?.close();
      return;
    }
    const url = URL.createObjectURL(
      new Blob([new Uint8Array(file.bytes)], { type: file.artifact.mimeType }),
    );
    if (tab) tab.location.href = url;
    window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }

  // The result tile (the old design notes "Results"): the live preview framed on the right,
  // Open as the white pill, Download and Open in new tab in the menu.
  return (
    <div>
      <MediaTile
        kind={tileKind}
        brand={tileKind === "document" ? t`Report` : (kindText ?? t`File`)}
        title={heading}
        badge={
          updating ? (
            <span
              data-testid="artifact-updating"
              className="ms-1.5 rounded-full bg-white/15 px-2 py-0.5 text-[11.5px] font-normal text-white motion-safe:animate-[rkPulse_1.6s_ease-in-out_infinite]"
            >
              {t`Updating`}
            </span>
          ) : superseded ? (
            <button
              type="button"
              data-testid="artifact-updated"
              onClick={() => studio?.open(superseded.artifactId, title)}
              className="ms-1.5 rounded-full bg-white/15 px-2 py-0.5 text-[11.5px] font-normal tabular-nums text-white hover:bg-white/25"
            >
              {t`Updated — v${superseded.version}`}
            </button>
          ) : versions > 1 ? (
            <span
              data-testid="artifact-version"
              className="ms-1.5 rounded-full bg-white/15 px-2 py-0.5 text-[11.5px] font-normal tabular-nums text-white"
            >
              {`v${version}`}
            </span>
          ) : undefined
        }
        art={
          <MediaFrame>
            <ArtifactInlinePreview
              artifactId={artifactId}
              name={data.name}
              kind={data.kind}
              size={data.size}
              version={data.version}
              onHeading={setDocumentTitle}
              onText={isDeck ? countSlides : undefined}
              className="h-full rounded-none border-0 sm:h-full"
            />
          </MediaFrame>
        }
        meta={failed ? t`Could not download this.` : meta}
        openLabel={t`Open`}
        openAriaLabel={t`Expand ${data.name}`}
        onOpen={expand}
        actions={
          <DropdownMenu>
            <DropdownMenuTrigger
              aria-label={t`More actions for ${data.name}`}
              className={MEDIA_ACTION}
            >
              <MoreHorizontal />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-auto min-w-40">
              {changeLook ? (
                <DropdownMenuItem data-testid="deck-change-look" onClick={changeLook}>
                  <Palette />
                  {t`Change look`}
                </DropdownMenuItem>
              ) : null}
              <DropdownMenuItem onClick={() => void download()}>
                <Download />
                {t`Download`}
              </DropdownMenuItem>
              {canOpenInNewTab ? (
                <DropdownMenuItem onClick={() => void openInNewTab()}>
                  <ExternalLink />
                  {t`Open in new tab`}
                </DropdownMenuItem>
              ) : null}
            </DropdownMenuContent>
          </DropdownMenu>
        }
      />
      {open ? (
        <Suspense fallback={null}>
          <ArtifactPreviewDialog
            artifactId={artifactId}
            onOpenChange={(next) => {
              if (!next) setOpen(false);
            }}
          />
        </Suspense>
      ) : null}
    </div>
  );
}

/** A reply card's `kind` label ("pdf", "html", "pptx", "png"…) as an artifact kind. */
function kindFromLabel(label: string): ArtifactKind {
  const value = label.toLowerCase();
  if (value === "pdf" || value === "md" || value === "markdown" || value === "docx")
    return "document";
  if (value === "html" || value === "page") return "page";
  if (value === "pptx" || value === "ppt" || value === "key" || value === "deck") return "deck";
  if (["png", "jpg", "jpeg", "gif", "webp", "svg", "image"].includes(value)) return "image";
  return "file";
}
