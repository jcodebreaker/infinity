// The artifacts capability's public entry point: what the capabilities built on it (apps, sheets)
// and the places that draw a file (transcript, Files tab) may use.

export { ArtifactFileCard } from "./ArtifactFileCard";
export { ArtifactPreview } from "./ArtifactPreview";
export { ArtifactPreviewThumbnail } from "./ArtifactPreviewThumbnail";
export {
  artifactsRevision,
  notifyArtifactsChanged,
  subscribeArtifactsChanged,
} from "./artifactChanges";
export { ArtifactFileCard as ReplyArtifactFileCard } from "./cards/ArtifactFileCard";
export { PanelOverflowMenu } from "./PanelOverflowMenu";
export {
  type ArtifactCardParts,
  type ArtifactEditorArgs,
  type ArtifactEditorHistory,
  type ArtifactEditorKind,
  type ArtifactEditorParts,
  type ArtifactExtension,
  type ArtifactInlineArgs,
  type ArtifactPanelArgs,
  type ArtifactPanelParts,
  ArtifactRegistryProvider,
  type ArtifactResultArgs,
  type ArtifactThumbnailArgs,
  type ArtifactViewProps,
  type LibraryFacet,
  type RegisterPublishSave,
  useArtifactCardParts,
  useArtifactExtensions,
  useArtifactLibraryFacets,
  useArtifactPanelParts,
  useArtifactPublishOptions,
} from "./registry";
