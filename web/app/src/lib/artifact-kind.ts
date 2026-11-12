import { t } from "@lingui/core/macro";
import { isAttachmentImageMimeType, isDeckArtifactName } from "@superchat/contracts";
import type { LucideIcon } from "lucide-react";
import { File, FileText, Image as ImageIcon, LayoutTemplate, Presentation } from "lucide-react";

/** How the Library (and any other artifact card) groups and labels an artifact, derived from its
 * mime type and, for decks, its name. */
export type ArtifactKind = "page" | "document" | "deck" | "image" | "file";

const DECK_MIME_TYPES = new Set([
  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  "application/vnd.ms-powerpoint",
  "application/vnd.google-apps.presentation",
  "application/vnd.apple.keynote",
]);

/** A slide deck is an HTML file named `*.deck.html`: the name says deck before the mime says page. */
export function artifactKind(mimeType: string, name?: string): ArtifactKind {
  if (name && isDeckArtifactName(name)) return "deck";
  if (mimeType === "text/html") return "page";
  if (mimeType === "application/pdf") return "document";
  if (DECK_MIME_TYPES.has(mimeType)) return "deck";
  if (isAttachmentImageMimeType(mimeType)) return "image";
  return "file";
}

/** Facet chip order; `LibraryScreen` only shows the ones with at least one artifact. */
export const KIND_ORDER: ArtifactKind[] = ["page", "document", "deck", "image", "file"];

export const KIND_ICON: Record<ArtifactKind, LucideIcon> = {
  page: LayoutTemplate,
  document: FileText,
  deck: Presentation,
  image: ImageIcon,
  file: File,
};

/** The quiet "Kind · size/time" meta line shown under a card's preview. */
export function kindLabel(kind: ArtifactKind): string {
  if (kind === "document") return "PDF";
  return kind.charAt(0).toUpperCase() + kind.slice(1);
}

/** The plural facet chip label. */
export function kindFacetLabel(kind: ArtifactKind): string {
  switch (kind) {
    case "page":
      return t`Pages`;
    case "document":
      return t`Documents`;
    case "deck":
      return t`Decks`;
    case "image":
      return t`Images`;
    case "file":
      return t`Files`;
    default:
      return kind;
  }
}

/** Kinds that get a real, rendered thumbnail (`ArtifactPreviewThumbnail`) rather than a type icon. */
export function isPreviewableArtifactKind(kind: ArtifactKind): boolean {
  return kind === "page" || kind === "image";
}
