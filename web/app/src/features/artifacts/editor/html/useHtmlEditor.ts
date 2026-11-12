// Portions modified from nexu-io/open-design apps/web/src/edit-mode/source-patches.ts@802708f, Apache-2.0; changes: the patches are applied by the engine (Python); this hook batches them into versions and keeps a version-aware undo/redo stack.
import type {
  Artifact,
  DeckEditFromFrame,
  DeckEditTarget,
  DeckEditTheme,
  HtmlPatch,
} from "@superchat/contracts";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { rpc } from "../../../../lib/rpc";
import type { ArtifactEditorHistory } from "../../registry";
import { registerEditorFlush, takeOwnVersion } from "../editor-state";
import { type CommitOutcome, Committer } from "./committer";
import {
  editConflict,
  isPathId,
  readTranslate,
  type StyleChanges,
  stylePatches,
  withTranslate,
} from "./edit-model";

/** The call the editor makes; the API by default (a fake in tests). */
export type HtmlEditSource = (input: {
  artifactId: string;
  baseVersion: number;
  patches: HtmlPatch[];
}) => Promise<Pick<Artifact, "id" | "version">>;

// The engine's HTML patch route (`PATCH /v1/artifacts/{id}/edit`): a deck or any HTML page.
const defaultSource: HtmlEditSource = (input) => rpc.artifacts.edit(input);

export type EditNotice = { kind: "stale" | "inexact" | "failed"; message: string };

type Kind = "edit" | "undo" | "redo";
const HISTORY_MAX = 50;

/** Posts a host message to the live frame (adds the protocol version and nonce). */
export type PostToFrame = (message: Record<string, unknown>) => void;

/**
 * The HTML editor's brain (a deck, a page, a mini app): what is selected, how changes become
 * saved versions, and undo / redo. It stays mounted while the file is open, so its history is the
 * file's one undo whether or not edit mode is on (an ask from the view, a Theme switch).
 * Every change shows at once in the frame (a preview) and is saved as a `manual` version through
 * one `Committer`; undo and redo save the earlier source back as a new version, so history is
 * always version-aware and a change from elsewhere ends it (the older sources would clobber it),
 * except the one superchat makes when asked from this editor, which Undo can step back over.
 */
export function useHtmlEditor({
  fileKey,
  active,
  artifactId,
  version,
  html,
  onEdited,
  post,
  locked = false,
  source = defaultSource,
}: {
  /** The file name: where outside edits and the flush hook are registered. */
  fileKey: string;
  /** Edit mode is on (the bridge frame is up). Leaving it saves what is staged. */
  active: boolean;
  artifactId: string;
  version: number;
  /** The source of the version on screen. */
  html: string;
  onEdited?: (artifactId: string) => void;
  post: PostToFrame;
  /** A studio turn is changing the file (ADR 0024): nothing staged by hand is saved. */
  locked?: boolean;
  source?: HtmlEditSource;
}) {
  const [targets, setTargets] = useState<DeckEditTarget[]>([]);
  const [theme, setTheme] = useState<DeckEditTheme | null>(null);
  const [notice, setNotice] = useState<EditNotice | null>(null);
  const [textEditing, setTextEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const [undoStack, setUndoStack] = useState<string[]>([]);
  const [redoStack, setRedoStack] = useState<string[]>([]);

  const undoStackRef = useRef(undoStack);
  undoStackRef.current = undoStack;
  const redoStackRef = useRef(redoStack);
  redoStackRef.current = redoStack;
  const saved = useRef({ id: artifactId, version });
  const seen = useRef({ version, html });
  const own = useRef(new Map<number, Kind>());
  const intent = useRef<Kind>("edit");
  // Superchat was asked from this editor: its next version joins the history (Undo steps back).
  const expectAsk = useRef(false);
  const selectedIds = useRef<string[]>([]);
  const targetsRef = useRef(targets);
  targetsRef.current = targets;
  const onEditedRef = useRef(onEdited);
  onEditedRef.current = onEdited;
  const sourceRef = useRef(source);
  sourceRef.current = source;
  const postRef = useRef(post);
  postRef.current = post;
  const lockedRef = useRef(locked);
  lockedRef.current = locked;

  // A new version on screen: ours extends the history, anyone else's ends it.
  useEffect(() => {
    if (version === seen.current.version) return;
    const before = seen.current.html;
    let kind = own.current.get(version);
    if (!kind && takeOwnVersion(fileKey, version)) {
      kind = "edit"; // the Theme gallery's switch: Undo steps back over it
      saved.current = { id: artifactId, version };
    } else if (!kind && expectAsk.current) {
      expectAsk.current = false;
      kind = "edit"; // superchat's change asked from here: Undo steps back over it too
      saved.current = { id: artifactId, version };
    }
    if (kind) {
      own.current.delete(version);
      if (kind === "edit") {
        setUndoStack((stack) => [...stack, before].slice(-HISTORY_MAX));
        setRedoStack([]);
      } else if (kind === "undo") setRedoStack((stack) => [...stack, before].slice(-HISTORY_MAX));
      else setUndoStack((stack) => [...stack, before].slice(-HISTORY_MAX));
    } else {
      setUndoStack([]);
      setRedoStack([]);
      saved.current = { id: artifactId, version };
    }
    seen.current = { version, html };
  }, [artifactId, version, html, fileKey]);

  const save = useCallback(async (patches: HtmlPatch[]): Promise<CommitOutcome> => {
    // A studio turn is changing the file (ADR 0024): no hand edit lands; the frame shows the
    // source again.
    if (lockedRef.current) {
      setReloadKey((n) => n + 1);
      return { ok: false, message: "" };
    }
    // An element the bridge could only address by its place in the page has no id in the file:
    // the engine would refuse it, so say so here and offer superchat instead.
    if (patches.some((p) => "id" in p && isPathId(p.id))) {
      const message = "no stable id; ask Superchat instead";
      setNotice({ kind: "inexact", message });
      setReloadKey((n) => n + 1);
      return { ok: false, message };
    }
    const base = saved.current;
    const kind: Kind = patches.some((p) => p.kind === "set-full-source") ? intent.current : "edit";
    intent.current = "edit";
    setSaving(true);
    try {
      const created = await sourceRef.current({
        artifactId: base.id,
        baseVersion: base.version,
        patches,
      });
      saved.current = { id: created.id, version: created.version };
      own.current.set(created.version, kind);
      setNotice(null);
      onEditedRef.current?.(created.id);
      return { ok: true };
    } catch (failure) {
      const message = failure instanceof Error ? failure.message : "";
      setNotice({ kind: editConflict(failure) ?? "failed", message });
      setReloadKey((n) => n + 1); // the preview no longer matches the source: show the source
      return { ok: false, message };
    } finally {
      setSaving(false);
    }
  }, []);

  const committer = useMemo(() => new Committer(save), [save]);
  // Closing the file saves whatever is still staged; so does leaving edit mode, which also ends
  // the selection (there is no frame to show it in).
  useEffect(() => () => void committer.commit(), [committer]);
  useEffect(() => {
    if (active) return;
    void committer.commit();
    setTargets([]);
    setTextEditing(false);
    selectedIds.current = [];
  }, [active, committer]);
  useEffect(
    () =>
      registerEditorFlush(fileKey, async () => {
        await committer.commit();
        return saved.current;
      }),
    [fileKey, committer],
  );

  const preview = useCallback((patches: readonly HtmlPatch[]) => {
    for (const patch of patches) {
      if (patch.kind === "set-style")
        postRef.current({ type: "superchat:edit-preview", id: patch.id, style: patch.style });
    }
  }, []);

  /** Style changes for the selection: shown now, saved after a quiet moment (or at once). */
  const applyStyle = useCallback(
    (changes: StyleChanges, mode: "later" | "now" = "later") => {
      const list = targetsRef.current;
      if (!list.length) return;
      const patches = stylePatches(list, changes);
      preview(patches);
      if (mode === "now") void committer.commit(patches);
      else committer.stage(patches);
    },
    [committer, preview],
  );

  const flush = useCallback(() => committer.commit(), [committer]);
  // A text edit in the frame, and who waits for it to end (`finish`).
  const textOpen = useRef(false);
  const textEnded = useRef<(() => void) | null>(null);
  /** Ends a text edit in progress (the frame sends its text first), then saves everything. */
  const finish = useCallback(async () => {
    if (textOpen.current) {
      await new Promise<void>((resolve) => {
        textEnded.current = resolve;
        postRef.current({ type: "superchat:edit-text-finish", commit: true });
        setTimeout(resolve, 1_000); // a frame that never answers
      });
    }
    await committer.commit();
  }, [committer]);
  const setExpectAsk = useCallback((on: boolean) => {
    expectAsk.current = on;
  }, []);

  const setAttributes = useCallback(
    (attributes: { href?: string | null; alt?: string | null }) => {
      const patches: HtmlPatch[] = targetsRef.current.map((t) => ({
        kind: "set-attributes",
        id: t.id,
        attributes,
      }));
      committer.stage(patches);
    },
    [committer],
  );

  const nudge = useCallback(
    (dx: number, dy: number) => {
      for (const t of targetsRef.current) {
        const at = readTranslate(t.inline.transform);
        const next = withTranslate(t.inline.transform, { x: at.x + dx, y: at.y + dy });
        const patches = stylePatches([t], { transform: next });
        preview(patches);
        committer.stage(patches);
      }
    },
    [committer, preview],
  );

  const removeSelected = useCallback(() => {
    const list = targetsRef.current;
    if (!list.length) return;
    // The last slide cannot go (the engine refuses it too); say so here without a round trip.
    void committer.commit(list.map((t) => ({ kind: "remove-element", id: t.id })));
    selectedIds.current = [];
  }, [committer]);

  const duplicateSelected = useCallback(() => {
    const list = targetsRef.current;
    if (!list.length) return;
    void committer.commit(list.map((t) => ({ kind: "duplicate-element", id: t.id })));
  }, [committer]);

  const undo = useCallback(async () => {
    await committer.commit();
    const previous = undoStackRef.current.at(-1);
    if (previous === undefined) return;
    setUndoStack((stack) => stack.slice(0, -1));
    intent.current = "undo";
    await committer.commit([{ kind: "set-full-source", source: previous }]);
  }, [committer]);

  const redo = useCallback(async () => {
    await committer.commit();
    const next = redoStackRef.current.at(-1);
    if (next === undefined) return;
    setRedoStack((stack) => stack.slice(0, -1));
    intent.current = "redo";
    await committer.commit([{ kind: "set-full-source", source: next }]);
  }, [committer]);
  const canUndo = undoStack.length > 0;
  const canRedo = redoStack.length > 0;
  /** The editor shell's undo / redo / save state for this file. */
  const history = useMemo<ArtifactEditorHistory>(
    () => ({
      canUndo,
      canRedo,
      saving,
      undo,
      redo,
      flush,
      finish,
      expectAsk: setExpectAsk,
      headVersion: () => saved.current.version,
    }),
    [canUndo, canRedo, saving, undo, redo, flush, finish, setExpectAsk],
  );

  /** The bridge's messages (already validated and nonce-checked by the stage). */
  const onFrameMessage = useCallback(
    (message: DeckEditFromFrame) => {
      switch (message.type) {
        case "superchat:edit-ready":
          setTheme(message.theme);
          break;
        case "superchat:edit-selection":
          setTargets(message.targets);
          selectedIds.current = message.targets.map((t) => t.id);
          break;
        case "superchat:edit-text-session":
          setTextEditing(message.active);
          textOpen.current = message.active;
          if (!message.active) textEnded.current?.();
          break;
        case "superchat:edit-text-commit":
          void committer.commit([{ kind: "set-text", id: message.id, text: message.text }]);
          break;
        case "superchat:edit-key":
          if (message.action === "undo") void undo();
          else if (message.action === "redo") void redo();
          else if (message.action === "delete") removeSelected();
          else if (message.action === "duplicate") duplicateSelected();
          else nudge(message.dx ?? 0, message.dy ?? 0);
          break;
      }
    },
    [committer, undo, redo, removeSelected, duplicateSelected, nudge],
  );

  return {
    targets,
    theme,
    notice,
    dismissNotice: () => setNotice(null),
    textEditing,
    reloadKey,
    selectedIds,
    head: saved,
    history,
    applyStyle,
    setAttributes,
    flush,
    /** Saves a whole new source now (a text document's block edit) as one step of the history;
     * `summary` says what changed. */
    saveSource: (source: string, summary?: string) =>
      committer.commit([{ kind: "set-full-source", source, summary }]),
    remove: removeSelected,
    duplicate: duplicateSelected,
    onFrameMessage,
    finishText: () => postRef.current({ type: "superchat:edit-text-finish", commit: true }),
    /** Select these elements (a layer picked in the navigator); kept for a frame not yet up. */
    select: (ids: string[], scroll = false) => {
      selectedIds.current = ids;
      postRef.current({ type: "superchat:edit-select", ids, ...(scroll ? { scroll } : {}) });
    },
  };
}
