import { useLingui } from "@lingui/react/macro";
import type { Artifact } from "@superchat/contracts";
import { Button, cn } from "@superchat/ui-web";
import { ChevronLeft, ChevronRight, Maximize2, Minimize2 } from "lucide-react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { SandboxedHtmlViewer } from "../../components/SandboxedHtmlViewer";
import type { ArtifactEditorArgs, ArtifactEditorKind, ArtifactEditorParts } from "../artifacts";
import { ArtifactEditorView, isTypingTarget } from "../artifacts/editor/ArtifactEditor";
import { artifactKey } from "../artifacts/editor/editor-state";
import { EditStage } from "../artifacts/editor/html/EditStage";
import { isPathId, targetLabel } from "../artifacts/editor/html/edit-model";
import { EditNoticeBanner, StylePanel } from "../artifacts/editor/html/StylePanel";
import {
  type HtmlEditSource,
  type PostToFrame,
  useHtmlEditor,
} from "../artifacts/editor/html/useHtmlEditor";
import { buildElementAsk, buildSlideAsk } from "../artifacts/editor/html-ask";
import { useDeckExport } from "./DeckExport";
import { DeckHeaderActions } from "./DeckHeaderActions";
import { DeckRail } from "./DeckRail";
import { DeckThemePicker, type DeckThemeSource, type DeckThemesSource } from "./DeckThemePicker";
import {
  type DeckNavigation,
  parseDeckSlides,
  postDeckNavigation,
  readDeckEvent,
} from "./deck-model";
import { setDeckThemeOpen, useDeckPresentRequest, useDeckThemeOpen } from "./deck-ui-state";

const BAR_HIDE_MS = 2500;

/** Where a deck's edits, theme dictionary and current theme come from: the API by default, a
 * recorder on the dev page and fakes in tests. */
export type DeckSources = {
  edit?: HtmlEditSource;
  themes?: DeckThemesSource;
  theme?: DeckThemeSource;
};
const DeckSourcesContext = createContext<DeckSources>({});

/**
 * A deck in the artifact editor: the sandboxed HTML on the stage (the deck scales itself to the
 * frame) with previous / next and full screen present mode, the slide rail as the navigator, the
 * edit bridge and style inspector in edit mode, and Theme / Present / Download in the header. The
 * deck keeps its own navigation state; the stage only sends `superchat:slide` commands and
 * mirrors the `superchat:slide-state` it hears back.
 */
function useDeckEditor({
  artifact,
  bytes,
  editing,
  onEdited,
  focusAsk,
  closeInspector,
  locked,
}: ArtifactEditorArgs): ArtifactEditorParts {
  const { t } = useLingui();
  const sources = useContext(DeckSourcesContext);
  const title = artifact.name;
  const key = artifactKey(artifact);
  const html = useMemo(() => new TextDecoder("utf-8").decode(bytes), [bytes]);
  const slides = useMemo(() => parseDeckSlides(html), [html]);
  const frame = useRef<HTMLIFrameElement | null>(null);
  const container = useRef<HTMLElement>(null);
  const [active, setActive] = useState(0);
  const [count, setCount] = useState(slides.length);
  const activeRef = useRef(0);
  activeRef.current = active;
  const [presenting, setPresenting] = useState(false);
  const [barVisible, setBarVisible] = useState(true);
  const barTimer = useRef<number | undefined>(undefined);
  const themeOpen = useDeckThemeOpen(key);
  const exporter = useDeckExport(artifact.id);
  // The gallery is a hidden state while presenting and must not outlive the deck on screen.
  useEffect(() => {
    if (presenting) setDeckThemeOpen(key, false);
  }, [presenting, key]);
  useEffect(() => () => setDeckThemeOpen(key, false), [key]);
  const presentRequest = useDeckPresentRequest(key);
  const handledPresent = useRef(presentRequest);

  const postRef = useRef<PostToFrame>(() => {});
  const post = useCallback<PostToFrame>((message) => postRef.current(message), []);
  const editMode = editing && !presenting;
  const editor = useHtmlEditor({
    fileKey: key,
    active: editMode,
    artifactId: artifact.id,
    version: artifact.version,
    html,
    onEdited,
    post,
    source: sources.edit,
    locked,
  });

  useEffect(() => {
    setCount(slides.length);
    setActive((value) => Math.min(value, Math.max(0, slides.length - 1)));
  }, [slides.length]);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.source !== frame.current?.contentWindow) return;
      const deckEvent = readDeckEvent(event.data);
      if (deckEvent?.kind === "state") {
        setActive(deckEvent.active);
        setCount(deckEvent.count);
      } else if (deckEvent?.kind === "ready" && activeRef.current > 0) {
        // A new version of the deck loaded: stay on the slide the person was looking at.
        postDeckNavigation(frame.current?.contentWindow, {
          action: "go",
          index: activeRef.current,
        });
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  const navigate = useCallback((nav: DeckNavigation) => {
    postDeckNavigation(frame.current?.contentWindow, nav);
  }, []);
  const goTo = useCallback((index: number) => navigate({ action: "go", index }), [navigate]);
  // A new edit frame is put on the slide on screen (once its bridge, or the deck, is ready).
  const place = useCallback((win: Window | null | undefined, foreign?: unknown) => {
    if (foreign !== undefined && readDeckEvent(foreign)?.kind !== "ready") return;
    postDeckNavigation(win, { action: "go", index: activeRef.current });
  }, []);

  const canFullscreen = typeof document !== "undefined" && !!document.fullscreenEnabled;
  const togglePresent = useCallback(() => {
    if (document.fullscreenElement) void document.exitFullscreen();
    else void container.current?.requestFullscreen?.();
  }, []);

  // The header's Present button asks by bumping a counter; only changes after mount count.
  useEffect(() => {
    if (presentRequest === handledPresent.current) return;
    handledPresent.current = presentRequest;
    if (canFullscreen && !document.fullscreenElement) void container.current?.requestFullscreen?.();
  }, [presentRequest, canFullscreen]);

  useEffect(() => {
    const onChange = () => setPresenting(document.fullscreenElement === container.current);
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
      if (isTypingTarget(event.target)) return;
      // A dialog the keyboard is in (a theme preview over this deck) owns the keys, unless it is
      // this deck's own (the Theme gallery) or holds this deck (the preview's deck).
      const dialog = (event.target as Element | null)?.closest?.('[role="dialog"]');
      const own = container.current;
      if (dialog && own && !dialog.contains(own) && !own.contains(dialog)) return;
      const nav: Record<string, DeckNavigation> = {
        ArrowRight: { action: "next" },
        PageDown: { action: "next" },
        ArrowLeft: { action: "prev" },
        PageUp: { action: "prev" },
        Home: { action: "first" },
        End: { action: "last" },
      };
      const next = nav[event.key];
      if (next) {
        event.preventDefault();
        navigate(next);
      } else if (event.key === "f" || event.key === "F") {
        if (!canFullscreen) return;
        event.preventDefault();
        togglePresent();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navigate, togglePresent, canFullscreen]);

  const revealBar = useCallback(() => {
    setBarVisible(true);
    window.clearTimeout(barTimer.current);
    barTimer.current = window.setTimeout(() => setBarVisible(false), BAR_HIDE_MS);
  }, []);
  useEffect(() => {
    if (presenting) revealBar();
    else setBarVisible(true);
    return () => window.clearTimeout(barTimer.current);
  }, [presenting, revealBar]);

  // Ask superchat: the selection, else the slide on screen.
  const words = {
    slide: t`Slide`,
    heading: t`Heading`,
    text: t`Text`,
    image: t`Image`,
    link: t`Link`,
    box: t`Box`,
  };
  const { targets, head } = editor;
  const contextBlock = useCallback(() => {
    const at = head.current;
    if (!targets.length) {
      return buildSlideAsk({
        artifactId: at.id,
        name: title,
        version: at.version,
        slide: active + 1,
      }).block;
    }
    return buildElementAsk({
      artifactId: at.id,
      name: title,
      version: at.version,
      noun: (n) => t`${n} elements`,
      elements: targets.map((target) => ({
        id: target.id,
        label: targetLabel(target, words),
        slide: target.slide,
        text: target.text,
        style: target.computed,
      })),
    }).block;
  }, [targets, head, title, active, t]);

  const showBar = !presenting || barVisible;
  const last = Math.max(0, count - 1);
  const bar = (
    <div
      className={cn(
        "flex shrink-0 items-center justify-center gap-1 px-3 py-2",
        presenting
          ? "absolute inset-x-0 bottom-4 mx-auto w-fit rounded-full bg-black/70 text-white motion-safe:transition-opacity"
          : "border-t border-border",
        !showBar && "pointer-events-none opacity-0",
      )}
    >
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label={t`Previous slide`}
        disabled={active <= 0}
        onClick={() => navigate({ action: "prev" })}
      >
        <ChevronLeft />
      </Button>
      <span
        data-testid="deck-counter"
        aria-live="polite"
        className="min-w-14 text-center text-[12.5px] tabular-nums"
      >
        {active + 1} / {Math.max(count, 1)}
      </span>
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label={t`Next slide`}
        disabled={active >= last}
        onClick={() => navigate({ action: "next" })}
      >
        <ChevronRight />
      </Button>
      {canFullscreen ? (
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label={presenting ? t`Exit present mode` : t`Present`}
          aria-pressed={presenting}
          onClick={togglePresent}
        >
          {presenting ? <Minimize2 /> : <Maximize2 />}
        </Button>
      ) : null}
    </div>
  );

  const stage = (
    <section
      ref={container}
      data-testid="deck-viewer"
      aria-label={title}
      onMouseMove={presenting ? revealBar : undefined}
      className={cn(
        "relative flex h-full min-h-0 flex-col bg-background",
        presenting && "bg-black",
      )}
    >
      <div className="relative min-h-0 flex-1">
        {editMode ? (
          <EditStage
            html={html}
            title={title}
            reloadKey={editor.reloadKey}
            place={place}
            selectedIds={editor.selectedIds}
            frameRef={frame}
            postRef={postRef}
            onMessage={editor.onFrameMessage}
          />
        ) : (
          <SandboxedHtmlViewer html={html} title={title} relay frameRef={frame} />
        )}
      </div>
      {bar}
      {themeOpen && !presenting ? (
        <DeckThemePicker
          deckKey={key}
          frameRef={frame}
          artifact={{ id: artifact.id, version: artifact.version }}
          onEdited={onEdited}
          themesSource={sources.themes}
          themeSource={sources.theme}
          editSource={sources.edit}
        />
      ) : null}
    </section>
  );

  const selected = targets.length > 0;
  return {
    label: t`Deck`,
    editable: true,
    stage,
    navigator: presenting ? null : (
      <DeckRail html={html} slides={slides} active={active} onSelect={goTo} />
    ),
    inspector: (
      <StylePanel
        targets={targets}
        theme={editor.theme}
        slideCount={editor.theme?.slideCount ?? 0}
        onStyle={(changes, mode) => editor.applyStyle(changes, mode)}
        onAttributes={editor.setAttributes}
        onCommit={() => {
          editor.finishText();
          void editor.flush();
        }}
        onClose={closeInspector}
        onRemove={editor.remove}
        onDuplicate={editor.duplicate}
        onAsk={focusAsk}
      />
    ),
    notice:
      editMode && editor.notice ? (
        <EditNoticeBanner
          notice={editor.notice}
          canAsk={!!artifact.botId && selected}
          onAsk={focusAsk}
          onDismissNotice={editor.dismissNotice}
        />
      ) : null,
    actions: (
      <DeckHeaderActions
        deckKey={key}
        state={exporter.state}
        onExport={exporter.run}
        onCancel={exporter.cancel}
        source={{ name: artifact.name, mimeType: artifact.mimeType, bytes }}
      />
    ),
    ownsDownload: true,
    history: editor.history,
    ask: {
      // The chip reads as the request will: `Slide 1 · cover-title`; with nothing selected, the
      // slide on screen.
      chips: targets.map((target) => ({
        id: target.id,
        label:
          target.kind === "slide"
            ? targetLabel(target, words)
            : `Slide ${target.slide} · ${isPathId(target.id) ? targetLabel(target, words) : target.id}`,
        detail: `${target.tag} · ${targetLabel(target, words)}`,
      })),
      onRemoveChip: (id: string) => editor.select(targets.map((x) => x.id).filter((x) => x !== id)),
      clear: () => editor.select([]),
      scope: `Slide ${active + 1}`,
      placeholder: selected
        ? t`Ask superchat to change this…`
        : t`Ask superchat to change this slide…`,
      contextBlock,
    },
  };
}

const noop = () => {};

/** A deck that is not a saved file as one (the dev page, a theme's sample, tests). */
function previewArtifact(
  at: { id: string; version: number },
  name: string,
  bytes: Uint8Array,
  botId: string | null,
): Artifact {
  return {
    id: at.id,
    version: at.version,
    name,
    mimeType: "text/html",
    botId,
    runId: null,
    description: null,
    size: bytes.byteLength,
    createdAt: "",
  };
}

/** A deck where the editor shell is not (a host file's preview): its rail and stage only,
 * read-only. */
export function DeckPreview({ html, title }: { html: string; title: string }) {
  const bytes = useMemo(() => new TextEncoder().encode(html), [html]);
  const artifact = previewArtifact({ id: "preview", version: 1 }, title, bytes, null);
  const parts = useDeckEditor({
    artifact,
    bytes,
    editing: false,
    setEditing: noop,
    locked: false,
    focusAsk: noop,
    closeInspector: noop,
  });
  return (
    <div className="flex h-full min-h-0 bg-background">
      {parts.navigator}
      <div className="min-w-0 flex-1">{parts.stage}</div>
    </div>
  );
}

/** Slide decks (HTML files named `*.deck.html`) in the artifact editor. */
export const deckEditorKind: ArtifactEditorKind = {
  id: "deck",
  useEditor: useDeckEditor,
};

/**
 * One deck in the editor shell without the version machinery: the dev page and tests, with
 * their own edit / theme sources.
 */
export function DeckViewer({
  html,
  title,
  artifact = { id: "preview", version: 1 },
  onEdited,
  editSource,
  themesSource,
  themeSource,
  botId = null,
}: {
  html: string;
  title: string;
  /** The saved file on screen (its id and version, for edits). */
  artifact?: { id: string; version: number };
  onEdited?: (artifactId: string) => void;
  editSource?: HtmlEditSource;
  themesSource?: DeckThemesSource;
  themeSource?: DeckThemeSource;
  /** The Muse that owns the deck: the ask box asks it. */
  botId?: string | null;
}) {
  const bytes = useMemo(() => new TextEncoder().encode(html), [html]);
  const sources = useMemo(
    () => ({ edit: editSource, themes: themesSource, theme: themeSource }),
    [editSource, themesSource, themeSource],
  );
  const file = previewArtifact(artifact, title, bytes, botId);
  return (
    <DeckSourcesContext.Provider value={sources}>
      <ArtifactEditorView artifact={file} bytes={bytes} kind={deckEditorKind} onEdited={onEdited} />
    </DeckSourcesContext.Provider>
  );
}
