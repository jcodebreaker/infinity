import { useLingui } from "@lingui/react/macro";
import type { Artifact, ArtifactVersion } from "@superchat/contracts";
import {
  Button,
  cn,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  Sheet,
  SheetContent,
  SheetTitle,
} from "@superchat/ui-web";
import {
  Download,
  ExternalLink,
  Maximize2,
  Minimize2,
  MousePointer2,
  Redo2,
  SlidersHorizontal,
  Undo2,
  X,
} from "lucide-react";
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { artifactKind, kindLabel } from "../../../lib/artifact-kind";
import { downloadArtifactBytes, opensInNewTab } from "../../../lib/artifact-open";
import { rpc } from "../../../lib/rpc";
import { useObjectUrl } from "../../../lib/use-object-url";
import { readableFileName } from "../ArtifactFileCard";
import { ArtifactPreview } from "../ArtifactPreview";
import { AskBox } from "../ask/AskBox";
import type { AskTurn } from "../ask/useArtifactAsk";
import { useArtifactAsk } from "../ask/useArtifactAsk";
import type { useArtifactContent } from "../library/useArtifactContent";
import { PanelOverflowMenu } from "../PanelOverflowMenu";
import {
  type ArtifactEditorArgs,
  type ArtifactEditorKind,
  type ArtifactEditorParts,
  useArtifactEditorKind,
  type useArtifactPanelParts,
} from "../registry";
import { isHandOrigin } from "./editor-state";
import { buildFileAsk } from "./html-ask";
import { NavigatorOrientationProvider } from "./NavigatorFrame";
import { VersionMenu } from "./VersionMenu";

export type ArtifactContent = ReturnType<typeof useArtifactContent>;
type PanelParts = ReturnType<typeof useArtifactPanelParts>;

/** What the frame around the editor (the File Studio, a dialog, the Library sheet) hands it. */
export type ArtifactEditorProps = {
  content: ArtifactContent;
  /** The registry's panel parts for this file (badges, Publish, bars, dialogs). */
  panel: PanelParts;
  /** A title the opener knows (a card's); the file's own title otherwise. */
  title?: string;
  /** Open a PDF at this page (a citation chip). */
  page?: number;
  /** At the start of the header: "Back", "‹ Library". */
  leading?: ReactNode;
  onClose: () => void;
  /** The frame can go full screen (a "Full screen" item in `⋯`). */
  fullScreen?: { on: boolean; toggle: () => void };
  /** The canvas opens selectable (hand editing) rather than viewing / running: the File Studio,
   * and the dev pages that drive a kind. Hand editing is entered by selecting; there is no
   * Edit or Done (ADR 0030). */
  startEditing?: boolean;
  /** The editor inside the File Studio (ADR 0030). */
  studio?: StudioSlot;
};

/** What the File Studio hands the editor it hosts. */
export type StudioSlot = {
  /** The studio chat session: the ask box sends there, never to the Conversation; null until
   * it is found or made (nothing can be sent meanwhile). */
  chatId: string | null;
  /** The file's conversation column, before the file, given the editor's ask box (null while
   * the file loads) to place at its bottom. */
  aside: (askBox: ReactNode) => ReactNode;
  /** The studio's turn, from its chat: while it runs no hand edit is saved. */
  turn: AskTurn;
  /** On a phone the editor stacks: the stage, the navigator as a strip, the inspector a sheet. */
  layout: "phone" | "wide";
  /** The canvas selection came or went: how to clear it while there is one, else null. */
  onSelection?: (clear: (() => void) | null) => void;
  /** Words a version's origin in the version menu. */
  versionOrigin?: (version: ArtifactVersion) => string;
  /** Why the studio's request waits (a runtime note): the ask box's status line meanwhile. */
  waiting?: string | null;
};

/** Focus is where the person types: keyboard shortcuts leave the key to it. */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

/** The file's display title: its own title, else a readable file name. */
export function artifactTitle(artifact: Pick<Artifact, "name" | "description">): string {
  return artifact.description?.trim() || readableFileName(artifact.name);
}

/**
 * The one editor for every artifact kind. The shell owns the header (title, "<Kind> · version N"
 * menu, the kind's actions, `⋯`, one close), the floating edit tools (undo, redo), the ask box
 * and the layout: navigator | stage (+ ask box) | inspector. A kind with an editor
 * (`ArtifactExtension.editor`: a deck, a page) fills the slots; every other file opens read-only
 * in the same shell, its preview on the stage and the ask box about the whole file. In the File
 * Studio the ask box goes to the conversation column (`studio.aside`), left of the file.
 */
export function ArtifactEditor(props: ArtifactEditorProps) {
  const { t } = useLingui();
  const { content } = props;
  const ready = content.status === "ready" ? content : null;
  const kind = useArtifactEditorKind(ready?.artifact);
  if (!ready) {
    const frame = (
      <EditorFrame
        {...props}
        heading={props.title || t`Loading…`}
        body={
          content.status === "error" ? (
            <div className="grid h-full place-items-center px-6 text-center text-[13.5px] text-destructive">
              {content.message}
            </div>
          ) : (
            <div className="grid h-full place-items-center text-[13.5px] text-muted-foreground">
              {t`Loading…`}
            </div>
          )
        }
      />
    );
    return props.studio ? (
      <>
        {props.studio.aside(null)}
        {frame}
      </>
    ) : (
      frame
    );
  }
  // Keyed by kind and file, not version: a new version keeps the mode, the selection and the
  // edit history instead of starting the editor over.
  const key = `${kind?.id ?? "file"}:${ready.artifact.name}`;
  return kind ? (
    <KindEditor key={key} {...props} artifact={ready.artifact} bytes={ready.bytes} kind={kind} />
  ) : (
    <FileEditor key={key} {...props} artifact={ready.artifact} bytes={ready.bytes} />
  );
}

type LoadedProps = ArtifactEditorProps & { artifact: Artifact; bytes: Uint8Array };

const NO_PANEL_PARTS: PanelParts = {
  badge: null,
  actions: null,
  below: null,
  overlay: null,
  overflow: null,
  versionNote: null,
  wide: false,
  holdsEscape: false,
  hasOverflow: false,
  hasVersionNote: false,
};

/**
 * One file in the editor without the version machinery or the registry's panel parts: the dev
 * page and tests drive a kind through the real shell this way.
 */
export function ArtifactEditorView({
  artifact,
  bytes,
  kind,
  onEdited,
  onClose = () => {},
}: {
  artifact: Artifact;
  bytes: Uint8Array;
  kind: ArtifactEditorKind;
  onEdited?: (artifactId: string) => void;
  onClose?: () => void;
}) {
  const content: ArtifactContent = {
    status: "ready",
    artifact,
    bytes,
    versions: [],
    versionId: artifact.id,
    selectVersion: () => {},
    adoptVersion: onEdited ?? (() => {}),
    newer: null,
    showNewest: () => {},
    arrived: null,
  };
  return (
    <KindEditor
      key={`${kind.id}:${artifact.name}`}
      content={content}
      panel={NO_PANEL_PARTS}
      onClose={onClose}
      startEditing
      artifact={artifact}
      bytes={bytes}
      kind={kind}
    />
  );
}

function KindEditor({ kind, ...props }: LoadedProps & { kind: ArtifactEditorKind }) {
  const { artifact } = props;
  // The file's own mode, kept across its versions (the editor is keyed by file, not version): a
  // kind leaves it to run (an app's Run) and comes back to it by selecting.
  const [editing, setEditing] = useState(() => !!props.studio || !!props.startEditing);
  const [askClicks, setAskClicks] = useState(0);
  const focusAsk = useCallback(() => setAskClicks((n) => n + 1), []);
  // The inspector is opened by hand (the canvas tools' Edit style); selecting never opens it.
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const closeInspector = useCallback(() => setInspectorOpen(false), []);
  const useEditor = kind.useEditor;
  const parts = useEditor({
    artifact,
    bytes: props.bytes,
    editing,
    setEditing,
    onEdited: props.content.adoptVersion,
    focusAsk,
    closeInspector,
    locked: !!props.studio?.turn.running,
  } satisfies ArtifactEditorArgs);
  return (
    <LoadedEditor
      {...props}
      parts={parts}
      editing={editing && parts.editable}
      setEditing={setEditing}
      inspectorOpen={inspectorOpen}
      setInspectorOpen={setInspectorOpen}
      askClicks={askClicks}
      focusAsk={focusAsk}
    />
  );
}

function FileEditor(props: LoadedProps) {
  const { t } = useLingui();
  const { artifact, bytes } = props;
  const [askClicks, setAskClicks] = useState(0);
  const focusAsk = useCallback(() => setAskClicks((n) => n + 1), []);
  const whole = t`Whole file`;
  const contextBlock = useCallback(
    () =>
      buildFileAsk({
        artifactId: artifact.id,
        name: artifact.name,
        version: artifact.version,
        label: whole,
      }).block,
    [artifact.id, artifact.name, artifact.version, whole],
  );
  const parts: ArtifactEditorParts = {
    label: kindLabel(artifactKind(artifact.mimeType, artifact.name)),
    editable: false,
    stage: (
      <ArtifactPreview
        artifact={artifact}
        bytes={bytes}
        onEdited={props.content.adoptVersion}
        page={props.page}
      />
    ),
    ask: {
      chips: [],
      scope: whole,
      placeholder: t`Ask superchat to change this file…`,
      contextBlock,
    },
  };
  return (
    <LoadedEditor
      {...props}
      parts={parts}
      editing={false}
      setEditing={() => {}}
      inspectorOpen={false}
      setInspectorOpen={() => {}}
      askClicks={askClicks}
      focusAsk={focusAsk}
    />
  );
}

function LoadedEditor({
  artifact,
  bytes,
  content,
  panel,
  parts,
  editing,
  setEditing,
  inspectorOpen,
  setInspectorOpen,
  askClicks,
  ...props
}: LoadedProps & {
  parts: ArtifactEditorParts;
  editing: boolean;
  setEditing: (on: boolean) => void;
  inspectorOpen: boolean;
  setInspectorOpen: (open: boolean) => void;
  askClicks: number;
  focusAsk: () => void;
}) {
  const { t } = useLingui();
  const url = useObjectUrl(bytes, artifact.mimeType);
  const { history } = parts;
  const botId = artifact.botId;
  const studio = props.studio;
  const ask = useArtifactAsk({
    // In the studio nothing is sent until its session is known: never to the Conversation.
    artifact:
      botId && (!studio || studio.chatId)
        ? {
            id: artifact.id,
            name: artifact.name,
            version: artifact.version,
            botId,
            chatId: studio?.chatId ?? undefined,
          }
        : null,
    contextBlock: parts.ask.contextBlock,
    turn: studio?.turn,
  });
  // While a studio turn runs the canvas stays readable, navigable and selectable (the next
  // request can be scoped); only hand edits wait: no inspector, and the kind saves nothing.
  const locked = !!studio?.turn.running;
  const phone = studio?.layout === "phone";
  const sendAsk = useCallback(
    async (text: string) => {
      await history?.flush(); // what is staged by hand is saved first, so superchat works on it
      history?.expectAsk(true);
      const sent = await ask.send(text, history?.headVersion());
      if (!sent) history?.expectAsk(false);
      return sent;
    },
    [history, ask.send],
  );
  // The ask settled (answered, failed, stopped): a version after this is not its answer.
  const expectAsk = history?.expectAsk;
  const askSettled = ask.status.kind !== "sending" && ask.status.kind !== "editing";
  useEffect(() => {
    if (askSettled) expectAsk?.(false);
  }, [askSettled, expectAsk]);

  // Undo / redo from the keyboard while focus is outside the frame (the bridge covers inside).
  const undo = history?.undo;
  const redo = history?.redo;
  useEffect(() => {
    if (!editing || !undo || !redo) return;
    const onKey = (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey) || isTypingTarget(event.target)) return;
      const key = event.key.toLowerCase();
      if (key === "z") {
        event.preventDefault();
        void (event.shiftKey ? redo() : undo());
      } else if (key === "y") {
        event.preventDefault();
        void redo();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [editing, undo, redo]);

  const restore = useCallback(async () => {
    if (!content.versionId) return;
    await history?.flush(); // what is staged by hand is saved first, so the restore comes after it
    const created = await rpc.artifacts.restore({ artifactId: content.versionId });
    content.adoptVersion(created.id);
  }, [content, history]);

  const actions = (
    <>
      {parts.actions}
      {panel.actions}
      {parts.ownsDownload ? null : (
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label={t`Download`}
          title={t`Download`}
          className="text-muted-foreground"
          onClick={() => downloadArtifactBytes(artifact.name, artifact.mimeType, bytes)}
        >
          <Download />
        </Button>
      )}
      <PanelOverflowMenu>
        <DropdownMenuGroup>
          <DropdownMenuLabel className="max-w-64 truncate font-normal text-muted-foreground">
            {artifact.name}
          </DropdownMenuLabel>
        </DropdownMenuGroup>
        {panel.overflow}
        {parts.overflow}
        {url && opensInNewTab(artifact.mimeType) ? (
          <DropdownMenuItem render={<a href={url} target="_blank" rel="noreferrer noopener" />}>
            <ExternalLink />
            {t`Open in new tab`}
          </DropdownMenuItem>
        ) : null}
        {props.fullScreen ? (
          <DropdownMenuItem onClick={props.fullScreen.toggle}>
            {props.fullScreen.on ? <Minimize2 /> : <Maximize2 />}
            {props.fullScreen.on ? t`Exit full screen` : t`Full screen`}
          </DropdownMenuItem>
        ) : null}
      </PanelOverflowMenu>
    </>
  );

  const meta = (
    <>
      <VersionMenu
        kindLabel={parts.label}
        version={artifact.version}
        versions={content.versions}
        shownId={content.versionId}
        onPick={content.selectVersion}
        onRestore={restore}
        note={panel.hasVersionNote ? panel.versionNote : undefined}
        originOf={studio?.versionOrigin}
      />
      {panel.badge ? (
        // Tags ("Published", "Live") give way first when the header is narrow; the bars below
        // still say both.
        <span className="flex shrink-0 items-center gap-2 @max-[18rem]:hidden">{panel.badge}</span>
      ) : null}
      {content.arrived ? (
        <span
          data-testid="artifact-arrived"
          className="shrink-0 truncate text-[12px] text-foreground"
        >
          {isHandOrigin(content.arrived.origin)
            ? t`Updated by you · v${content.arrived.version}`
            : t`Updated by superchat · v${content.arrived.version}`}
        </span>
      ) : null}
      {content.newer ? (
        <button
          type="button"
          data-testid="artifact-newer"
          onClick={content.showNewest}
          className="shrink-0 rounded-full bg-accent px-2 py-0.5 text-[12px] text-foreground hover:bg-accent/70"
        >
          {t`Newer version available, show it`}
        </button>
      ) : null}
    </>
  );

  const selected = parts.ask.chips.length > 0;
  const clearSelection = () => {
    if (parts.ask.clear) parts.ask.clear();
    else for (const chip of parts.ask.chips) parts.ask.onRemoveChip?.(chip.id);
  };
  const clearRef = useRef(clearSelection);
  clearRef.current = clearSelection;
  const onSelection = studio?.onSelection;
  useEffect(
    () => onSelection?.(selected ? () => clearRef.current() : null),
    [onSelection, selected],
  );
  // The canvas tools, a pill above it: Select (the canvas is selectable, hand editing is entered by
  // selecting; off, the file runs or plays), then undo and redo. There is no Edit and no Done.
  // Turning Select off first saves a text edit still in progress; a failed save stays selectable
  // with its notice.
  const toggleSelect = () =>
    editing
      ? void (history?.finish?.() ?? Promise.resolve()).then(
          () => setEditing(false),
          () => {},
        )
      : setEditing(true);
  const toolbar = parts.editable ? (
    <div className="flex shrink-0 justify-center px-3 pt-2.5 pb-1">
      <div
        role="toolbar"
        aria-label={t`Canvas tools`}
        data-testid="editor-toolbar"
        className="superchat-glass-pill flex items-center gap-0.5 rounded-full p-1"
      >
        <Button
          variant="ghost"
          size="icon-sm"
          aria-pressed={editing}
          aria-label={t`Select`}
          title={t`Select`}
          data-testid="editor-select"
          className="rounded-full aria-pressed:bg-selection-strong aria-pressed:text-tint-ink"
          onClick={toggleSelect}
        >
          <MousePointer2 />
        </Button>
        {editing && parts.inspector ? (
          <Button
            variant="ghost"
            size="icon-sm"
            aria-pressed={inspectorOpen}
            aria-label={t`Edit style`}
            title={t`Edit style`}
            data-testid="editor-inspector-toggle"
            disabled={locked}
            className="rounded-full aria-pressed:bg-selection-strong aria-pressed:text-tint-ink"
            onClick={() => setInspectorOpen(!inspectorOpen)}
          >
            <SlidersHorizontal />
          </Button>
        ) : null}
        {editing && history ? (
          <>
            <span aria-hidden="true" className="mx-0.5 h-4 w-px bg-separator" />
            <Button
              variant="ghost"
              size="icon-sm"
              className="rounded-full"
              aria-label={t`Undo`}
              title={t`Undo (⌘Z)`}
              disabled={!history.canUndo || history.saving}
              onClick={() => void history.undo()}
            >
              <Undo2 />
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              className="rounded-full"
              aria-label={t`Redo`}
              title={t`Redo (⇧⌘Z)`}
              disabled={!history.canRedo || history.saving}
              onClick={() => void history.redo()}
            >
              <Redo2 />
            </Button>
          </>
        ) : null}
      </div>
    </div>
  ) : null;

  const askBox = ask.available ? (
    <AskBox
      className={studio ? undefined : "mx-auto max-w-[760px]"}
      chips={parts.ask.chips}
      scope={parts.ask.scope}
      onRemoveChip={parts.ask.onRemoveChip}
      placeholder={parts.ask.placeholder}
      focusRequest={askClicks + (parts.ask.focusRequest ?? 0)}
      status={ask.status}
      onSend={sendAsk}
      onStop={() => void ask.stop()}
      onUndo={
        history
          ? () => {
              ask.dismiss();
              void history.undo();
            }
          : undefined
      }
      canUndo={history?.canUndo}
      note={studio?.waiting}
    />
  ) : null;
  const showInspector = editing && !!parts.inspector && !locked && inspectorOpen;
  // Escape closes the open inspector before anything else (the selection, the studio): capture
  // phase, so it runs ahead of the studio's own Escape.
  useEffect(() => {
    if (!showInspector) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      const target = event.target instanceof Element ? event.target : null;
      if (isTypingTarget(target) || target?.closest('[role="menu"],[role="dialog"]')) return;
      event.preventDefault();
      setInspectorOpen(false);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [showInspector, setInspectorOpen]);
  const inspector = !showInspector ? null : phone ? (
    // A phone's inspector is a sheet over the bottom of the canvas; the canvas stays live under it.
    <Sheet
      open
      modal={false}
      disablePointerDismissal
      onOpenChange={(open) => {
        if (!open) setInspectorOpen(false);
      }}
    >
      <SheetContent
        side="bottom"
        showCloseButton={false}
        data-testid="editor-inspector"
        className="max-h-[70%] gap-0 p-2"
      >
        <SheetTitle className="sr-only">{t`Selection`}</SheetTitle>
        {parts.inspector}
      </SheetContent>
    </Sheet>
  ) : (
    // From about 900px wide the open inspector is a column beside the stage, so nothing is
    // covered. Narrower, the stage keeps its width and the controls are a sheet over the bottom
    // edge.
    <div
      data-testid="editor-inspector"
      data-slot="editor-inspector"
      className={cn(
        "absolute inset-x-2 bottom-2 z-20 flex max-h-[60%] justify-end",
        "@[900px]:static @[900px]:inset-auto @[900px]:block @[900px]:max-h-none @[900px]:w-[300px] @[900px]:shrink-0 @[900px]:overflow-hidden",
      )}
    >
      <div className="flex h-full max-h-full w-[288px] @[900px]:w-[300px] @[900px]:py-3 @[900px]:pe-3">
        {parts.inspector}
      </div>
    </div>
  );

  const body = (
    <NavigatorOrientationProvider value={phone ? "horizontal" : "vertical"}>
      <div
        data-slot="editor-body"
        className={cn("@container relative flex min-h-0 flex-1", phone && "flex-col")}
      >
        {phone ? null : parts.navigator}
        <div data-slot="editor-stage" className="relative flex min-h-0 min-w-0 flex-1 flex-col">
          {toolbar}
          <div className="relative min-h-0 flex-1">
            <div data-slot="editor-canvas" className="contents">
              {parts.stage}
            </div>
            {locked ? <WorkingCanvas /> : null}
          </div>
          {parts.footer}
          <div className="relative">
            {parts.notice ? (
              <div className="pointer-events-none absolute inset-x-3 bottom-full z-10 mb-2 flex justify-center">
                <div className="pointer-events-auto">{parts.notice}</div>
              </div>
            ) : null}
            {askBox && !studio ? <div className="px-4 pt-1 pb-3">{askBox}</div> : null}
          </div>
        </div>
        {phone ? parts.navigator : null}
        {inspector}
      </div>
    </NavigatorOrientationProvider>
  );

  const frame = (
    <EditorFrame
      {...props}
      content={content}
      panel={panel}
      heading={props.title || artifactTitle(artifact)}
      fileName={artifact.name}
      meta={meta}
      actions={actions}
      body={body}
    />
  );
  return studio ? (
    <>
      {studio.aside(askBox)}
      {frame}
    </>
  ) : (
    frame
  );
}

/** The canvas while a studio turn changes the file: the work strip's lit edge (ADR 0023) over
 * it, nothing in the way; the turn's words are in the conversation's live strip. */
function WorkingCanvas() {
  return (
    <div data-testid="studio-working" className="pointer-events-none absolute inset-2 z-10">
      <span aria-hidden="true" className="sc-live-ring" data-shape="canvas" />
    </div>
  );
}

/** The header row and the body under it. */
function EditorFrame({
  heading,
  fileName,
  meta,
  actions,
  body,
  leading,
  panel,
  onClose,
  studio,
}: ArtifactEditorProps & {
  heading: string;
  fileName?: string;
  meta?: ReactNode;
  actions?: ReactNode;
  body: ReactNode;
}) {
  const { t } = useLingui();
  return (
    <div data-testid="artifact-editor" className="flex min-h-0 min-w-0 flex-1 flex-col">
      <header
        data-slot="editor-header"
        className={cn(
          "flex min-w-0 shrink-0 items-center gap-1.5 border-b border-border px-4 py-2.5",
          studio?.layout === "phone" && "flex-wrap gap-y-1.5",
        )}
      >
        {leading}
        <div
          data-slot="editor-heading"
          className={cn(
            "@container flex min-w-0 flex-1 items-center gap-2 overflow-hidden",
            studio?.layout === "phone" && "basis-[55%]",
          )}
        >
          <h2
            data-testid="artifact-editor-title"
            title={fileName}
            dir="auto"
            className="min-w-16 truncate text-[14px] font-semibold"
          >
            {heading}
          </h2>
          {meta}
        </div>
        {actions}
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label={studio ? t`Close and go back to Super Chat` : t`Close`}
          data-testid="editor-close"
          className="text-muted-foreground"
          onClick={onClose}
        >
          <X />
        </Button>
      </header>
      {panel.below}
      {panel.overlay}
      {body}
    </div>
  );
}
