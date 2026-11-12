import { Trans, useLingui } from "@lingui/react/macro";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@superchat/ui-web";
import { Download, Ellipsis, Maximize2 } from "lucide-react";
import { Fragment } from "react";
import { artifactKind, kindLabel } from "../../../lib/artifact-kind";
import { formatRelativeTime } from "../../../lib/relative-time";
import { readableFileName } from "../ArtifactFileCard";
import { ArtifactPreviewCard, GlassAction, glassActionClassName } from "../ArtifactPreviewCard";
import { useArtifactCardParts } from "../registry";
import { LibraryArt } from "./LibraryArt";
import type { ArtifactSummary } from "./types";

export function LibraryCard({
  artifact,
  onOpen,
  onDownload,
  onDelete,
}: {
  artifact: ArtifactSummary;
  onOpen: () => void;
  onDownload: () => void;
  onDelete: () => void;
}) {
  const { t } = useLingui();
  const kind = artifactKind(artifact.mimeType, artifact.name);
  // The title the Muse gave it when saving, else its file name made readable ("tea.deck.html"
  // reads "Tea").
  const title = artifact.description?.trim() || readableFileName(artifact.name);
  const extra = useArtifactCardParts(artifact);
  const badges = extra.filter((part) => part.badge);
  const meta = [
    `${kindLabel(kind)} · ${formatRelativeTime(artifact.createdAt)}`,
    ...extra.map((part) => part.meta),
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <ArtifactPreviewCard
      size="grid"
      artifact={artifact}
      title={title}
      meta={meta}
      badge={
        badges.length ? (
          badges.map((part, index) => <Fragment key={index}>{part.badge}</Fragment>)
        ) : kind === "page" && !artifact.publish ? (
          <span
            data-testid="private-pill"
            className="inline-flex shrink-0 items-center rounded-full bg-popover/85 px-2 py-0.5 text-[11px] font-medium text-foreground shadow-xs ring-1 ring-border backdrop-blur-sm"
          >
            {t`Private`}
          </span>
        ) : null
      }
      buttonLabel={t`Open ${title}`}
      onOpen={onOpen}
      testId="library-card"
      fallback={<LibraryArt kind={kind} name={title} />}
      actions={
        <>
          <GlassAction label={t`Open ${title}`} onClick={onOpen}>
            <Maximize2 size={13} strokeWidth={1.9} />
          </GlassAction>
          <GlassAction label={t`Download ${title}`} onClick={onDownload}>
            <Download size={13} strokeWidth={1.9} />
          </GlassAction>
          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <button
                  type="button"
                  aria-label={t`More actions for ${title}`}
                  onClick={(event) => event.stopPropagation()}
                  className={glassActionClassName}
                />
              }
            >
              <Ellipsis size={13} strokeWidth={1.9} />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem variant="destructive" onClick={onDelete}>
                <Trans>Delete</Trans>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </>
      }
    />
  );
}
