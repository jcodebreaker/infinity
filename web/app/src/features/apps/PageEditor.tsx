import { useLingui } from "@lingui/react/macro";
import { type Artifact, isDeckArtifactName } from "@superchat/contracts";
import { Button, cn } from "@superchat/ui-web";
import { Lock, Play, RotateCcw } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { SandboxedHtmlViewer } from "../../components/SandboxedHtmlViewer";
import type { ArtifactEditorArgs, ArtifactEditorKind, ArtifactEditorParts } from "../artifacts";
import { artifactKey, useArtifactFocus, useArtifactMarked } from "../artifacts/editor/editor-state";
import { EditStage } from "../artifacts/editor/html/EditStage";
import { isPathId, targetLabel } from "../artifacts/editor/html/edit-model";
import { markParts, tokenColor } from "../artifacts/editor/html/mark-parts";
import { EditNoticeBanner, StylePanel } from "../artifacts/editor/html/StylePanel";
import { type PostToFrame, useHtmlEditor } from "../artifacts/editor/html/useHtmlEditor";
import { buildElementAsk, buildFileAsk } from "../artifacts/editor/html-ask";
import { NavigatorFrame } from "../artifacts/editor/NavigatorFrame";

/** The tags a page's layers are made of. */
const LANDMARKS = new Set([
  "header",
  "nav",
  "main",
  "section",
  "article",
  "aside",
  "footer",
  "form",
]);
const MAX_LAYERS = 40;

export type PageLayer = { id: string; label: string; tag: string; depth: number };

/** "add-form" -> "Add form". */
const humanize = (id: string) => {
  const words = id.replace(/[-_]+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : id;
};

/**
 * A page's layers: its landmark and section elements that carry a `data-superchat-id` (so the
 * bridge can select them by it), in document order, one level of nesting shown.
 */
export function pageLayers(html: string): PageLayer[] {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const nodes = Array.from(doc.body.querySelectorAll<HTMLElement>("[data-superchat-id]")).filter(
    (el) => LANDMARKS.has(el.tagName.toLowerCase()),
  );
  const listed = new Set<Element>(nodes);
  const layers: PageLayer[] = [];
  for (const el of nodes) {
    let depth = 0;
    for (let up = el.parentElement; up; up = up.parentElement) if (listed.has(up)) depth += 1;
    if (depth > 1) continue;
    const id = el.getAttribute("data-superchat-id") ?? "";
    const label = el.getAttribute("aria-label")?.trim() || humanize(id);
    layers.push({ id, label, tag: el.tagName.toLowerCase(), depth });
    if (layers.length === MAX_LAYERS) break;
  }
  return layers;
}

/** A page with scripts is an app (it runs); one without is a page (it is previewed). */
export const isAppHtml = (html: string): boolean => /<script\b/i.test(html);

/**
 * An HTML page or mini app in the artifact editor. Run (or Preview) shows it live and
 * interactive; Edit puts the same edit bridge decks use on it: click any element to select it
 * (a chip in the ask box), double-click text to type, style it (Edit style), and every change
 * is a patch the engine saves as a new version. An element with no `data-superchat-id` can be
 * selected and asked about, not changed by hand. The preview keeps its sandbox and CSP in both.
 */
function usePageEditor({
  artifact,
  bytes,
  editing,
  setEditing,
  onEdited,
  focusAsk,
  closeInspector,
  locked,
}: ArtifactEditorArgs): ArtifactEditorParts {
  const { t } = useLingui();
  const title = artifact.name;
  const html = useMemo(() => new TextDecoder("utf-8").decode(bytes), [bytes]);
  const app = isAppHtml(html);
  const fileKey = artifactKey(artifact);
  const marked = useArtifactMarked(fileKey);
  const markColor = marked.length ? tokenColor("--link") : "";
  const shown = useMemo(() => markParts(html, marked, markColor), [html, marked, markColor]);
  const layers = useMemo(() => pageLayers(html), [html]);
  const frame = useRef<HTMLIFrameElement | null>(null);
  const postRef = useRef<PostToFrame>(() => {});
  const post = useCallback<PostToFrame>((message) => postRef.current(message), []);
  const [runKey, setRunKey] = useState(0);
  const editor = useHtmlEditor({
    fileKey,
    active: editing,
    artifactId: artifact.id,
    version: artifact.version,
    html,
    onEdited,
    post,
    locked,
  });
  const { targets, head } = editor;
  const words = {
    heading: t`Heading`,
    image: t`Image`,
    link: t`Link`,
  };
  const label = (target: (typeof targets)[number]) => targetLabel(target, words, false);
  const shortText = (text: string | null) => (text ?? "").replace(/\s+/g, " ").trim().slice(0, 24);
  const chipName = (target: (typeof targets)[number]) =>
    isPathId(target.id) ? shortText(target.text) : target.id;

  const whole = t`Whole page`;
  const contextBlock = useCallback(() => {
    const at = head.current;
    if (!targets.length) {
      return buildFileAsk({ artifactId: at.id, name: title, version: at.version, label: whole })
        .block;
    }
    return buildElementAsk({
      artifactId: at.id,
      name: title,
      version: at.version,
      scope: "page",
      noun: (n) => t`${n} elements`,
      elements: targets.map((target) => ({
        id: target.id,
        label: `${target.tag}${chipName(target) ? ` · ${chipName(target)}` : ""}`,
        slide: 1,
        text: target.text,
        style: target.computed,
        where: target.where,
      })),
    }).block;
  }, [targets, head, title, whole, t]);

  const pickLayer = (id: string) => {
    editor.select([id]);
    if (!editing) setEditing(true);
  };
  // A part asked for by another capability (an update's change chip): select it and scroll to it.
  const wanted = useArtifactFocus(fileKey);
  const handled = useRef(wanted?.n ?? 0);
  const scrollTo = useRef<string | null>(null);
  const selectEditor = editor.select;
  useEffect(() => {
    if (!wanted || wanted.n === handled.current) return;
    handled.current = wanted.n;
    scrollTo.current = editing ? null : wanted.id;
    selectEditor([wanted.id], true);
    setEditing(true);
  }, [wanted, editing, selectEditor, setEditing]);
  // The edit frame opens after the request: scroll once it is ready.
  const onStageReady = useCallback(() => {
    if (!scrollTo.current) return;
    postRef.current({ type: "superchat:edit-select", ids: [scrollTo.current], scroll: true });
    scrollTo.current = null;
  }, []);
  const selected = targets.length > 0;
  const shownIds = new Set(targets.map((target) => target.id));

  const stage = (
    <div data-testid="page-stage" className="relative h-full bg-background">
      {editing ? (
        <EditStage
          html={shown}
          title={title}
          mode="page"
          reloadKey={editor.reloadKey}
          onReady={onStageReady}
          selectedIds={editor.selectedIds}
          frameRef={frame}
          postRef={postRef}
          onMessage={editor.onFrameMessage}
        />
      ) : (
        <SandboxedHtmlViewer key={runKey} html={shown} title={title} frameRef={frame} />
      )}
      <Lock
        size={12}
        aria-label={t`Isolated preview`}
        className="pointer-events-none absolute end-3 bottom-3 text-muted-foreground"
      />
    </div>
  );

  return {
    label: app ? t`App` : t`Page`,
    editable: true,
    stage,
    navigator: layers.length ? (
      <NavigatorFrame
        label={t`Layers`}
        showLabel={t`Show layers`}
        hideLabel={t`Hide layers`}
        testId="page-layers"
      >
        <ul
          aria-label={t`Layers`}
          className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto px-2 pt-1 pb-3"
        >
          {layers.map((layer) => (
            <li key={layer.id}>
              <button
                type="button"
                data-testid="page-layer"
                aria-current={shownIds.has(layer.id) ? "true" : undefined}
                title={`${layer.tag} · ${layer.id}`}
                onClick={() => pickLayer(layer.id)}
                className={cn(
                  "flex h-7 w-full items-center truncate rounded-md px-2 text-start text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  layer.depth ? "ps-5" : "",
                  shownIds.has(layer.id)
                    ? "bg-accent font-medium text-foreground"
                    : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
                )}
              >
                <span className="truncate">{layer.label}</span>
              </button>
            </li>
          ))}
        </ul>
      </NavigatorFrame>
    ) : null,
    inspector: (
      <StylePanel
        inDeck={false}
        targets={targets}
        theme={editor.theme}
        slideCount={0}
        onStyle={(changes, mode) => editor.applyStyle(changes, mode)}
        onAttributes={editor.setAttributes}
        onCommit={() => void editor.history.finish?.()}
        onClose={closeInspector}
        onRemove={editor.remove}
        onDuplicate={editor.duplicate}
        onAsk={artifact.botId ? focusAsk : undefined}
      />
    ),
    notice:
      editing && editor.notice ? (
        <EditNoticeBanner
          notice={editor.notice}
          canAsk={!!artifact.botId && selected}
          onAsk={focusAsk}
          onDismissNotice={editor.dismissNotice}
        />
      ) : null,
    // Run (Preview for a page without scripts) is the live, interactive file; pressed while it
    // is what is on screen, where it starts the app again.
    actions: (
      <Button
        variant={editing ? "secondary" : "outline"}
        size="sm"
        aria-pressed={!editing}
        data-testid="page-run"
        title={editing ? undefined : app ? t`Run again` : t`Reload`}
        onClick={() => (editing ? setEditing(false) : setRunKey((n) => n + 1))}
      >
        {editing ? <Play size={13} /> : <RotateCcw size={13} />}
        {app ? t`Run` : t`Preview`}
      </Button>
    ),
    history: editor.history,
    ask: {
      chips: targets.map((target) => ({
        id: target.id,
        label: chipName(target) ? `${target.tag} · ${chipName(target)}` : target.tag,
        detail: target.where ?? label(target),
      })),
      onRemoveChip: (id: string) => editor.select(targets.map((x) => x.id).filter((x) => x !== id)),
      clear: () => editor.select([]),
      scope: whole,
      placeholder: selected
        ? t`Ask superchat to change this…`
        : app
          ? t`Ask superchat to change this app…`
          : t`Ask superchat to change this page…`,
      contextBlock,
    },
  };
}

/** HTML pages and mini apps (every `text/html` file that is not a deck) in the artifact editor. */
export const pageEditorKind: ArtifactEditorKind = {
  id: "page",
  useEditor: usePageEditor,
};

/** Whether a file opens as a page in the editor. */
export const isPageArtifact = (artifact: Artifact): boolean =>
  artifact.mimeType === "text/html" && !isDeckArtifactName(artifact.name);
