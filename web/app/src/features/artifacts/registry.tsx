import type { Artifact, ArtifactVersion } from "@superchat/contracts";
import { createContext, Fragment, type ReactNode, useContext } from "react";
import type { AskChip } from "./ask/AskBox";

/**
 * What a capability built on artifacts (apps, sheets) adds to the artifact surfaces without the
 * artifact code importing it: `Shell` fills the registry, `ArtifactPanel` and `LibraryCard` read
 * it. The list is fixed for the life of the app, so calling each `usePanel` is hook-safe.
 */
export interface ArtifactPanelParts {
  /** Beside the title (e.g. a "Published" pill). */
  badge?: ReactNode;
  /** Header buttons, before the download/close buttons. */
  actions?: ReactNode;
  /** A bar under the header. */
  below?: ReactNode;
  /** A dialog over the panel. */
  overlay?: ReactNode;
  /** The panel is wider for this file (a sheet). */
  wide?: boolean;
  /** True while an overlay should take Escape instead of closing the panel. */
  holdsEscape?: boolean;
  /** `DropdownMenuItem`s for the header's `⋯` menu. */
  overflow?: ReactNode;
  /** A quiet line at the bottom of the version menu ("Last checked Mon · nothing new"). */
  versionNote?: ReactNode;
}

/** What `usePanel` is given about the file being shown. */
export interface ArtifactPanelArgs {
  artifact: Artifact | undefined;
  title?: string;
  /** The shown version's bytes, once loaded. */
  bytes?: Uint8Array;
  /** The page's newest version, when the list has loaded. */
  newestVersion?: ArtifactVersion | null;
}

export interface ArtifactCardParts {
  /** Appended to the card's meta line. */
  meta?: string;
  badge?: ReactNode;
}

export interface ArtifactViewProps {
  artifact: Artifact;
  bytes: Uint8Array;
  onEdited?: (artifactId: string) => void;
  /** What to show when the viewer cannot (e.g. the file is not readable as a table). */
  fallback: ReactNode;
}

export interface ArtifactExtension {
  /** Called as a hook for every panel render; `artifact` is undefined until the file loads. */
  usePanel(args: ArtifactPanelArgs): ArtifactPanelParts;
  /** Plain function for a Library card. */
  card(artifact: Artifact): ArtifactCardParts | null;
  /** Like `card`, for parts that follow data which loads or changes (called as a hook). */
  useCard?(artifact: Artifact): ArtifactCardParts | null;
  /** A Library filter chip of its own ("Live"), or null while it has nothing to show. `matches`
   * says which files it keeps. Called as a hook. */
  useLibraryFacet?(): LibraryFacet | null;
  /** A viewer for this file, or null to leave it to the default preview. */
  view?(props: ArtifactViewProps): ReactNode | null;
  /** A live preview for a chat file card's body (a chart), or null to leave it to the default.
   * `near` is true once the card is close to the viewport, so it can fetch lazily. */
  inline?(props: ArtifactInlineArgs): ReactNode | null;
  /** Draws the whole chat result in place of the file card (a chart drawn as a chart), or null
   * to keep the card. */
  result?(props: ArtifactResultArgs): ReactNode | null;
  /** The Library thumbnail for this file, or null to leave it to the default preview. */
  thumbnail?(artifact: ArtifactThumbnailArgs): ReactNode | null;
  /** The editor kind for this file (a deck, a page), or null: the file then opens in the same
   * editor shell read-only, with its `view` (or the default preview) on the stage. */
  editor?(artifact: Artifact): ArtifactEditorKind | null;
  /** A field for the publish dialog. `register` hands the dialog a save to run when the person
   * confirms; it returns the unregister. */
  publishOptions?(artifact: Artifact, register: RegisterPublishSave): ReactNode;
}

export interface LibraryFacet {
  /** Stable, never one of the file kinds ("live"). */
  value: string;
  label: string;
  matches(artifact: Artifact): boolean;
}

export type RegisterPublishSave = (save: () => Promise<void>) => () => void;

/**
 * One kind of file in the artifact editor (`editor/ArtifactEditor`). The shell owns the frame —
 * header (title, "<Kind> · version N" menu, actions, `⋯`, close), the canvas tools (select,
 * undo, redo), the ask box and the responsive layout — and the kind fills the slots.
 * `useEditor` is called as a hook for as long as the file is open (the shell remounts it when the
 * kind changes), so it can keep the file's edit history across edit mode on and off.
 */
export interface ArtifactEditorKind {
  /** Stable per kind ("deck", "page"). */
  id: string;
  useEditor(args: ArtifactEditorArgs): ArtifactEditorParts;
}

export interface ArtifactEditorArgs {
  artifact: Artifact;
  bytes: Uint8Array;
  /** Edit mode (select, type, style) rather than viewing / running. */
  editing: boolean;
  setEditing: (on: boolean) => void;
  /** A version was saved from the editor: show it. */
  onEdited?: (artifactId: string) => void;
  /** Puts the keyboard in the ask box (an "Ask superchat instead"). */
  focusAsk: () => void;
  /** Closes the inspector (it opens only from the canvas tools, never on a selection). */
  closeInspector: () => void;
  /** A studio turn is changing the file (ADR 0024): no hand edit is saved until it ends. */
  locked: boolean;
}

export interface ArtifactEditorParts {
  /** What the header calls this file: "Deck", "App", "Page". */
  label: string;
  stage: ReactNode;
  /** The left column (a deck's slides, a page's layers); collapsible. */
  navigator?: ReactNode;
  /** The right column, shown in edit mode while the person has it open (the canvas tools'
   * Edit style); a selection alone never opens it. */
  inspector?: ReactNode;
  /** Header buttons (a deck: Theme, Present, Download; a page: Run). */
  actions?: ReactNode;
  /** `DropdownMenuItem`s for the header's `⋯` menu. */
  overflow?: ReactNode;
  /** The kind draws its own Download (a deck's export menu), so the shell leaves its own out. */
  ownsDownload?: boolean;
  /** Between the stage and the ask box (a deck's slide navigation). */
  footer?: ReactNode;
  /** Over the ask box (a save that failed). */
  notice?: ReactNode;
  /** The file can be edited: the shell shows the canvas tools (and the inspector on demand). */
  editable: boolean;
  /** The file's one undo: the toolbar, ⌘Z and the ask box's "Updated · Undo" all use it. */
  history?: ArtifactEditorHistory;
  ask: {
    /** The selection, as chips in the ask box. */
    chips: readonly AskChip[];
    onRemoveChip?: (id: string) => void;
    /** Ends the whole selection at once (Escape in the studio). */
    clear?: () => void;
    /** What a request is about with nothing selected ("Slide 2"): a quiet chip in its place. */
    scope?: string;
    placeholder: string;
    /** The request block for what is selected (or on screen) right now; a kind may read it from
     * the engine (a sheet's range excerpt). */
    contextBlock: () => string | Promise<string>;
    /** Bumped to focus the ask box (the selection chip's own Ask). */
    focusRequest?: number;
  };
}

export interface ArtifactEditorHistory {
  canUndo: boolean;
  canRedo: boolean;
  /** A save is in flight. */
  saving: boolean;
  undo: () => Promise<void>;
  redo: () => Promise<void>;
  /** Saves what is staged; before an ask, so superchat works on it. */
  flush: () => Promise<void>;
  /** Ends a text edit still in progress and saves it with the rest (before leaving edit mode). */
  finish?: () => Promise<void>;
  /** The next outside version is superchat's answer to an ask from here (Undo steps over it). */
  expectAsk: (on: boolean) => void;
  /** The newest version this editor saved (may not be on screen yet). */
  headVersion: () => number;
}

export interface ArtifactInlineArgs {
  artifactId: string;
  name: string;
  version?: number;
  near: boolean;
}

export interface ArtifactResultArgs {
  artifactId: string;
  name: string;
  version?: number;
  /** The reply card's data, for a fallback to the plain card. */
  data: Record<string, unknown>;
}

export interface ArtifactThumbnailArgs {
  id: string;
  name: string;
  mimeType: string;
  version?: number;
  /** Reports a short fact read from the file once its bytes are here (a deck's "4 slides"),
   * for the card's meta line. Must be stable. */
  onMeta?: (meta: string) => void;
}

const ArtifactRegistryContext = createContext<readonly ArtifactExtension[]>([]);
export const ArtifactRegistryProvider = ArtifactRegistryContext.Provider;
export const useArtifactExtensions = () => useContext(ArtifactRegistryContext);

/** Every extension's panel parts, merged in registry order. */
export function useArtifactPanelParts(
  args: ArtifactPanelArgs,
): Required<ArtifactPanelParts> & { hasOverflow: boolean; hasVersionNote: boolean } {
  const extensions = useArtifactExtensions();
  // The registry never changes after Shell mounts it, so the hook order is stable.
  // biome-ignore lint/correctness/useHookAtTopLevel: a fixed list, called in the same order
  const parts = extensions.map((extension) => extension.usePanel(args));
  const slot = (pick: (part: ArtifactPanelParts) => ReactNode) =>
    parts.map((part, index) => <Fragment key={index}>{pick(part)}</Fragment>);
  return {
    badge: slot((part) => part.badge),
    actions: slot((part) => part.actions),
    below: slot((part) => part.below),
    overlay: slot((part) => part.overlay),
    wide: parts.some((part) => part.wide),
    holdsEscape: parts.some((part) => part.holdsEscape),
    overflow: slot((part) => part.overflow),
    versionNote: slot((part) => part.versionNote),
    hasOverflow: parts.some((part) => !!part.overflow),
    hasVersionNote: parts.some((part) => !!part.versionNote),
  };
}

/** The editor kind for a file: the first extension that has one. */
export function useArtifactEditorKind(artifact: Artifact | undefined): ArtifactEditorKind | null {
  const extensions = useArtifactExtensions();
  if (!artifact) return null;
  for (const extension of extensions) {
    const kind = extension.editor?.(artifact);
    if (kind) return kind;
  }
  return null;
}

/** The publish dialog's extra fields from every extension, given the dialog's `register`. */
export function useArtifactPublishOptions(
  artifact: Artifact | undefined,
): (register: RegisterPublishSave) => ReactNode {
  const extensions = useArtifactExtensions();
  return (register) =>
    artifact
      ? extensions.map((extension, index) => (
          <Fragment key={index}>{extension.publishOptions?.(artifact, register)}</Fragment>
        ))
      : null;
}

/** Every extension's card parts for one artifact. */
export function useArtifactCardParts(artifact: Artifact): ArtifactCardParts[] {
  const extensions = useArtifactExtensions();
  // The registry never changes after Shell mounts it, so the hook order is stable.
  // biome-ignore lint/correctness/useHookAtTopLevel: a fixed list, called in the same order
  const hooked = extensions.map((extension) => extension.useCard?.(artifact) ?? null);
  return extensions
    .map((extension, index) => extension.card(artifact) ?? hooked[index] ?? null)
    .filter((part): part is ArtifactCardParts => part !== null);
}

/** The Library filter chips the extensions add. */
export function useArtifactLibraryFacets(): LibraryFacet[] {
  return (
    useArtifactExtensions()
      // biome-ignore lint/correctness/useHookAtTopLevel: a fixed list, called in the same order
      .map((extension) => extension.useLibraryFacet?.() ?? null)
      .filter((facet): facet is LibraryFacet => facet !== null)
  );
}
