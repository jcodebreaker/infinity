import { type Artifact, isDeckArtifactName } from "@superchat/contracts";
import type { ArtifactExtension, ArtifactPanelArgs, ArtifactPanelParts } from "../artifacts";
import { artifactKey } from "../artifacts/editor/editor-state";
import { DeckCover } from "./DeckCover";
import { DeckPreview, deckEditorKind } from "./DeckViewer";
import { useDeckThemeOpen } from "./deck-ui-state";
import { PptxPreview } from "./PptxPreview";
import { isPptxArtifact } from "./pptx-normalize";

/** A deck needs room for its rail and stage; the Theme gallery takes Escape while it is open. */
function useDecksPanel({ artifact }: ArtifactPanelArgs): ArtifactPanelParts {
  const deck: Artifact | undefined =
    artifact && isDeckArtifactName(artifact.name) ? artifact : undefined;
  const themeOpen = useDeckThemeOpen(deck ? artifactKey(deck) : "");
  if (!deck) return { wide: !!artifact && isPptxArtifact(artifact) };
  return { wide: true, holdsEscape: themeOpen };
}

/** Opens slide decks (HTML files named `*.deck.html`) in the artifact editor as the deck kind,
 * and PowerPoint files (`.pptx`, such as the Muse's native-chart decks) in a read-only preview. */
export const decksExtension: ArtifactExtension = {
  usePanel: useDecksPanel,
  // The Library already labels a deck "Deck" (lib/artifact-kind).
  card: () => null,
  thumbnail: (artifact) =>
    isDeckArtifactName(artifact.name) || isPptxArtifact(artifact) ? (
      <DeckCover
        id={artifact.id}
        name={artifact.name}
        version={artifact.version}
        onMeta={artifact.onMeta}
      />
    ) : null,
  // Where the editor shell is not (a host file's preview): a deck still reads as a deck.
  view: ({ artifact, bytes, fallback }) =>
    isPptxArtifact(artifact) ? (
      <PptxPreview bytes={bytes} fallback={fallback} />
    ) : isDeckArtifactName(artifact.name) ? (
      <DeckPreview html={new TextDecoder("utf-8").decode(bytes)} title={artifact.name} />
    ) : null,
  editor: (artifact) => (isDeckArtifactName(artifact.name) ? deckEditorKind : null),
};
