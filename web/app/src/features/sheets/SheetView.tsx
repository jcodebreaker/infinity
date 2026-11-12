import { useLingui } from "@lingui/react/macro";
import type { Artifact, ArtifactTable, TableCell, TableCellEdit } from "@superchat/contracts";
import { cn } from "@superchat/ui-web";
import {
  createContext,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { rpc } from "../../lib/rpc";
import {
  type ArtifactEditorArgs,
  type ArtifactEditorHistory,
  type ArtifactEditorKind,
  type ArtifactEditorParts,
  ArtifactPreview,
} from "../artifacts";
import { ArtifactEditorView } from "../artifacts/editor/ArtifactEditor";
import { artifactKey, useArtifactFocus, useArtifactMarked } from "../artifacts/editor/editor-state";
import { buildFileAsk } from "../artifacts/editor/html-ask";
import { NavigatorFrame } from "../artifacts/editor/NavigatorFrame";
import { createEvaluator } from "./formula-eval";
import {
  type CellPos,
  type CellRange,
  cellAt,
  cellFromValue,
  cellKey,
  cellSource,
  cellText,
  colLetter,
  diffTables,
  inRange,
  isNumeric,
  normalizeRange,
  parseCellId,
  parseDraft,
  rangeCellCount,
  rangeLabel,
  rangeSum,
  revertEdits,
} from "./sheet-model";

const ROW_H = 30;
const HEAD_H = 30;
const GUTTER_W = 48;
const COL_W = 140;
const COL_MIN_W = 48;
const COL_MAX_W = 520;
/** Cell padding (px-2.5 both sides) + the border + a little air. */
const CELL_PAD = 32;
/** The grid body font (text-[13px] in the page's sans). */
const CELL_FONT_SIZE = 13;
/** Average glyph width at the grid font when there is no canvas to measure with. */
const FALLBACK_CHAR_PX = 7.6;
const MEASURE_ROWS = 500;

let measureCtx: CanvasRenderingContext2D | null | undefined;
/** Pixel width of `text` at the grid font, or null where there is no canvas (tests). */
function measureText(text: string, weight: number): number | null {
  if (measureCtx === undefined) {
    try {
      measureCtx = document.createElement("canvas").getContext("2d");
    } catch {
      measureCtx = null;
    }
  }
  if (!measureCtx) return null;
  const family = getComputedStyle(document.body).fontFamily || "sans-serif";
  measureCtx.font = `${weight} ${CELL_FONT_SIZE}px ${family}`;
  return measureCtx.measureText(text).width;
}

/** A column's width in pixels: wide enough for its longest value (the file's character width, or
 * what the cells measure at the grid font, whichever is larger) plus padding, within the cap. */
function columnPx(
  chars: number | null | undefined,
  texts: readonly string[],
  bold: number,
): number {
  const fromChars = chars ? chars * FALLBACK_CHAR_PX : 0;
  let widest = 0;
  let measured = false;
  texts.forEach((text, index) => {
    const px = measureText(text, index < bold ? 600 : 400);
    if (px !== null) {
      measured = true;
      widest = Math.max(widest, px);
    }
  });
  const content = measured ? Math.max(widest, chars ? fromChars * 0.6 : 0) : fromChars;
  if (!chars && !measured) return COL_W;
  return Math.max(COL_MIN_W, Math.min(COL_MAX_W, Math.ceil(content + CELL_PAD)));
}
const OVERSCAN = 8;
/** Used until the grid is measured (and where there is no layout, e.g. tests). */
const FALLBACK_VIEW_H = 640;
/** The most cells one save may change (the engine's limit). */
const MAX_EDITS = 500;
const HISTORY_MAX = 50;

/** The calls the editor makes; the API by default (a fixture in the dev preview). */
export interface SheetSource {
  table(input: { artifactId: string }): Promise<ArtifactTable>;
  listVersions(input: {
    familyId: string;
  }): Promise<Array<{ id: string; origin?: string; parentVersionId?: string | null }>>;
  editTable(input: {
    artifactId: string;
    baseVersion: number;
    edits: TableCellEdit[];
  }): Promise<{ id: string; version: number }>;
  tableRange(input: {
    artifactId: string;
    sheet: string;
    range: string;
  }): Promise<{ block: string }>;
}

/** What the version history says about the table on screen. */
type History = {
  /** The newest version: older ones are read-only. */
  newest: boolean;
  /** Cells changed by hand since the last version Superchat saved. */
  changed: Set<string>;
};

const NO_HISTORY: History = { newest: true, changed: new Set() };

type EditKind = "edit" | "undo" | "redo";

const isConflict = (error: unknown): boolean =>
  typeof error === "object" && error !== null && (error as { code?: unknown }).code === "CONFLICT";

/** The table calls live on `sheets`, the version list on `artifacts`. */
const defaultSource: SheetSource = {
  table: (input) => rpc.sheets.table(input),
  editTable: (input) => rpc.sheets.editTable(input),
  tableRange: (input) => rpc.sheets.tableRange(input),
  listVersions: (input) => rpc.artifacts.listVersions(input),
};

const SheetSourceContext = createContext<SheetSource>(defaultSource);

/**
 * A CSV / XLSX in the artifact editor: the grid on the stage, its sheets in the navigator.
 * Selecting cells puts them in the ask box (the engine's excerpt of the range is the request
 * block) and, in edit mode, their sum, count and average in the inspector. Behind the shell's
 * Edit every cell change saves at once as a new version; undo and redo send the earlier values
 * back the same way, so the history is the shell's one undo.
 */
function useSheetEditor({
  artifact,
  bytes,
  editing: editMode,
  onEdited,
}: ArtifactEditorArgs): ArtifactEditorParts {
  const { t } = useLingui();
  const source = useContext(SheetSourceContext);
  const cells = (n: number) => (n === 1 ? t`1 cell` : t`${n} cells`);
  const [table, setTable] = useState<ArtifactTable | null>(null);
  const [failed, setFailed] = useState(false);
  const [history, setHistory] = useState<History>(NO_HISTORY);
  const [sheetIndex, setSheetIndex] = useState(0);
  const [overlay, setOverlay] = useState<ReadonlyMap<string, TableCell>>(new Map());
  const [editing, setEditing] = useState<{ r: number; c: number; draft: string } | null>(null);
  // Whether the person has selected cells (the ask box then asks about them).
  const [picked, setPicked] = useState(false);
  const [anchor, setAnchor] = useState<CellPos>({ r: 0, c: 0 });
  const [head, setHead] = useState<CellPos>({ r: 0, c: 0 });
  const [error, setError] = useState<string | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewH, setViewH] = useState(FALLBACK_VIEW_H);
  const [undoStack, setUndoStack] = useState<TableCellEdit[][]>([]);
  const [redoStack, setRedoStack] = useState<TableCellEdit[][]>([]);
  const [saving, setSaving] = useState(false);
  const undoRef = useRef(undoStack);
  undoRef.current = undoStack;
  const redoRef = useRef(redoStack);
  redoRef.current = redoStack;
  const scroller = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);
  const settled = useRef(false);
  const loadSeq = useRef(0);
  const headVersion = useRef({ id: artifact.id, version: artifact.version });
  const queue = useRef<Promise<void>>(Promise.resolve());
  const pending = useRef(0);
  const shownTable = useRef<ArtifactTable | null>(null);
  // Superchat was asked from here: its next version joins the history (Undo steps back over it).
  const expectAsk = useRef(false);

  const requestedId = useRef<string | null>(null);
  const load = useCallback(
    async (id: string, own: boolean) => {
      const seq = ++loadSeq.current;
      requestedId.current = id;
      // Read now: the ask settles (and clears this) as soon as the new version is on screen.
      const asked = !own && expectAsk.current;
      if (!own) expectAsk.current = false;
      try {
        const next = await source.table({ artifactId: id });
        const versions = await source.listVersions({ familyId: id }).catch(() => []);
        const info = await readHistory(source, id, next, versions);
        if (seq !== loadSeq.current) return;
        if (!own) {
          // Someone else's version ends the history (the old values would clobber it), except
          // superchat's answer to an ask from here, which Undo can put back.
          const before = shownTable.current;
          const revert = asked && before ? revertEdits(diffTables(before.sheets, next.sheets)) : [];
          setUndoStack((stack) =>
            revert.length > 0 && revert.length <= MAX_EDITS
              ? [...stack, revert].slice(-HISTORY_MAX)
              : [],
          );
          setRedoStack([]);
        }
        shownTable.current = next;
        headVersion.current = { id: next.artifactId, version: next.version };
        setTable(next);
        setHistory(info);
        setOverlay(new Map());
        setFailed(false);
      } catch {
        if (seq === loadSeq.current) setFailed(true);
      }
    },
    [source],
  );

  useEffect(() => {
    // A version this editor just saved is already loading.
    if (requestedId.current !== artifact.id) void load(artifact.id, false);
  }, [artifact.id, load]);

  useEffect(() => {
    const node = scroller.current;
    if (node && node.clientHeight > 0) setViewH(node.clientHeight);
  }, [table]);

  // Leaving edit mode drops a cell that is being typed into.
  useEffect(() => {
    if (!editMode) setEditing(null);
  }, [editMode]);

  const sheet = table?.sheets[Math.min(sheetIndex, (table?.sheets.length ?? 1) - 1)];
  const sheetName = sheet?.name ?? "";
  const colCount = Math.max(sheet?.rows[0]?.length ?? 0, 1);
  const rowCount = Math.max(sheet?.rows.length ?? 0, 1);
  const frozen = Math.min(sheet?.frozenRows ?? 0, rowCount);
  // Column widths: each column fits its longest value (header row included) at the grid font.
  const colLeft = useMemo(() => {
    const lefts = [0];
    const rows = sheet?.rows.slice(0, MEASURE_ROWS) ?? [];
    for (let c = 0; c < colCount; c += 1) {
      const texts = rows.map(
        (_, r) => cellText(cellAt(sheet as NonNullable<typeof sheet>, r, c)).text,
      );
      lefts.push(
        (lefts[c] as number) +
          columnPx(sheet?.colWidths?.[c], texts, Math.min(sheet?.frozenRows ?? 0, rows.length)),
      );
    }
    return lefts;
  }, [colCount, sheet]);
  const colWidth = (c: number) => (colLeft[c + 1] as number) - (colLeft[c] as number);

  // Formulas the file holds no result for (or whose inputs were just edited) are worked out here.
  const evaluate = useMemo(
    () => createEvaluator(table?.sheets ?? [], (name, r, c) => overlay.get(cellKey(name, r, c))),
    [table, overlay],
  );
  /** A cell of any sheet as it now stands (an unsaved edit, else the file, formulas worked out). */
  const cellIn = useCallback(
    (name: string, r: number, c: number): TableCell => {
      const raw =
        overlay.get(cellKey(name, r, c)) ??
        cellAt(table?.sheets.find((entry) => entry.name === name) ?? EMPTY_SHEET, r, c);
      if (!raw.f) return raw;
      const computed = evaluate(name, r, c);
      return computed === undefined
        ? raw
        : { ...raw, v: computed, t: typeof computed === "number" ? "n" : raw.t };
    },
    [overlay, table, evaluate],
  );
  const cell = useCallback(
    (r: number, c: number): TableCell => cellIn(sheetName, r, c),
    [cellIn, sheetName],
  );

  const selection: CellRange = useMemo(() => normalizeRange(anchor, head), [anchor, head]);

  // Cells another capability marks (what a live update changed) and asks to be shown (its chip).
  const fileKey = artifactKey(artifact);
  const markedIds = useArtifactMarked(fileKey);
  const firstSheet = table?.sheets[0]?.name ?? "";
  const marked = useMemo(() => {
    const here = new Set<string>();
    for (const id of markedIds) {
      const at = parseCellId(id);
      if (at && (at.sheet ?? firstSheet) === sheetName) here.add(`${at.r}:${at.c}`);
    }
    return here;
  }, [markedIds, firstSheet, sheetName]);
  const wanted = useArtifactFocus(fileKey);
  const shownFocus = useRef(wanted?.n ?? 0);
  useEffect(() => {
    if (!wanted || wanted.n === shownFocus.current || !table) return;
    shownFocus.current = wanted.n;
    const at = parseCellId(wanted.id);
    const index = at?.sheet ? table.sheets.findIndex((entry) => entry.name === at.sheet) : 0;
    if (!at || index < 0) return;
    setSheetIndex(index);
    setAnchor({ r: at.r, c: at.c });
    setHead({ r: at.r, c: at.c });
    setPicked(true);
  }, [wanted, table]);

  // --- saving -------------------------------------------------------------------------------

  const save = useCallback(
    async (edits: TableCellEdit[]) => {
      try {
        let created: { id: string; version: number };
        const base = headVersion.current;
        try {
          created = await source.editTable({
            artifactId: base.id,
            baseVersion: base.version,
            edits,
          });
        } catch (failure) {
          if (!isConflict(failure)) throw failure;
          // Someone saved in between: take the newest version and apply the change once more.
          const newest = (await source.listVersions({ familyId: base.id }))[0];
          if (!newest) throw failure;
          const fresh = await source.table({ artifactId: newest.id });
          created = await source.editTable({
            artifactId: newest.id,
            baseVersion: fresh.version,
            edits,
          });
        }
        headVersion.current = { id: created.id, version: created.version };
        onEdited?.(created.id);
        await load(created.id, true);
      } catch {
        setError(t`Couldn't save that change.`);
        await load(headVersion.current.id, true);
      }
    },
    [load, onEdited, source, t],
  );

  /** Shows the edits at once and saves them as a new version; the values they replace go on the
   * undo stack (an edit, a redo) or the redo stack (an undo). */
  const commit = useCallback(
    (edits: TableCellEdit[], kind: EditKind = "edit") => {
      if (edits.length === 0) return;
      setError(null);
      const before = edits.map((edit) => {
        const now = cellIn(edit.sheet, edit.row, edit.col);
        return { ...edit, value: now.f ?? now.v };
      });
      if (kind === "undo") setRedoStack((stack) => [...stack, before].slice(-HISTORY_MAX));
      else setUndoStack((stack) => [...stack, before].slice(-HISTORY_MAX));
      if (kind === "edit") setRedoStack([]);
      setOverlay((current) => {
        const next = new Map(current);
        for (const edit of edits) {
          next.set(
            cellKey(edit.sheet, edit.row, edit.col),
            cellFromValue(edit.value, cellIn(edit.sheet, edit.row, edit.col)),
          );
        }
        return next;
      });
      pending.current += 1;
      setSaving(true);
      queue.current = queue.current
        .then(() => save(edits))
        .finally(() => {
          pending.current -= 1;
          if (pending.current === 0) setSaving(false);
        });
    },
    [cellIn, save],
  );
  const commitRef = useRef(commit);
  commitRef.current = commit;

  const step = useCallback(async (kind: "undo" | "redo") => {
    await queue.current;
    const stack = kind === "undo" ? undoRef : redoRef;
    const edits = stack.current.at(-1);
    if (!edits) return;
    stack.current = stack.current.slice(0, -1);
    (kind === "undo" ? setUndoStack : setRedoStack)(stack.current);
    commitRef.current(edits, kind);
    await queue.current;
  }, []);
  const undo = useCallback(() => step("undo"), [step]);
  const redo = useCallback(() => step("redo"), [step]);
  const flush = useCallback(() => queue.current, []);
  const setExpectAsk = useCallback((on: boolean) => {
    expectAsk.current = on;
  }, []);
  const canUndo = undoStack.length > 0;
  const canRedo = redoStack.length > 0;
  const editHistory = useMemo<ArtifactEditorHistory>(
    () => ({
      canUndo,
      canRedo,
      saving,
      undo,
      redo,
      flush,
      expectAsk: setExpectAsk,
      headVersion: () => headVersion.current.version,
    }),
    [canUndo, canRedo, saving, undo, redo, flush, setExpectAsk],
  );

  // --- selection and keys -------------------------------------------------------------------

  const clamp = useCallback(
    (pos: CellPos): CellPos => ({
      r: Math.max(0, Math.min(rowCount - 1, pos.r)),
      c: Math.max(0, Math.min(colCount - 1, pos.c)),
    }),
    [colCount, rowCount],
  );

  function select(pos: CellPos, extend: boolean) {
    const next = clamp(pos);
    if (!extend) setAnchor(next);
    setHead(next);
    setPicked(true);
  }

  function pickSheet(index: number) {
    setSheetIndex(index);
    setAnchor({ r: 0, c: 0 });
    setHead({ r: 0, c: 0 });
    setPicked(false);
    setEditing(null);
    if (scroller.current) scroller.current.scrollTop = 0;
    setScrollTop(0);
  }

  function finishEdit(save: boolean, move?: { dr: number; dc: number }) {
    if (!editing) return;
    const { r, c, draft } = editing;
    setEditing(null);
    if (save && sheet && draft !== cellSource(cell(r, c))) {
      commit([{ sheet: sheetName, row: r, col: c, value: parseDraft(draft) }]);
    }
    if (move) select({ r: r + move.dr, c: c + move.dc }, false);
    window.requestAnimationFrame(() => scroller.current?.focus());
  }

  const canEdit = history.newest;
  const editOn = editMode && canEdit;

  function onGridKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (editing || !sheet) return;
    const step = (dr: number, dc: number) => {
      event.preventDefault();
      const from = event.shiftKey ? head : anchor;
      select({ r: from.r + dr, c: from.c + dc }, event.shiftKey);
    };
    switch (event.key) {
      case "ArrowDown":
        return step(1, 0);
      case "ArrowUp":
        return step(-1, 0);
      case "ArrowRight":
        return step(0, 1);
      case "ArrowLeft":
        return step(0, -1);
      case "Tab":
        return step(0, event.shiftKey ? -1 : 1);
    }
    if (!editOn) return;
    if (event.key === "Enter" || event.key === "F2") {
      event.preventDefault();
      setEditing({ r: anchor.r, c: anchor.c, draft: cellSource(cell(anchor.r, anchor.c)) });
    } else if (event.key === "Delete" || event.key === "Backspace") {
      event.preventDefault();
      const edits: TableCellEdit[] = [];
      for (let r = selection.r0; r <= selection.r1; r += 1) {
        for (let c = selection.c0; c <= selection.c1; c += 1) {
          if (cellSource(cell(r, c)) !== "") {
            edits.push({ sheet: sheetName, row: r, col: c, value: null });
          }
        }
      }
      commit(edits.slice(0, MAX_EDITS));
    } else if (event.key.length === 1 && !event.metaKey && !event.ctrlKey && !event.altKey) {
      event.preventDefault();
      setEditing({ r: anchor.r, c: anchor.c, draft: event.key });
    }
  }

  const posOf = (event: ReactMouseEvent): CellPos | null => {
    const td = (event.target as HTMLElement).closest<HTMLElement>("[data-r][data-c]");
    return td ? { r: Number(td.dataset.r), c: Number(td.dataset.c) } : null;
  };

  function onMouseDown(event: ReactMouseEvent) {
    const pos = posOf(event);
    if (!pos || (event.target as HTMLElement).tagName === "INPUT") return;
    if (editing) finishEdit(true);
    dragging.current = true;
    select(pos, event.shiftKey);
    scroller.current?.focus({ preventScroll: true });
  }

  useEffect(() => {
    const stop = () => {
      dragging.current = false;
    };
    window.addEventListener("mouseup", stop);
    return () => window.removeEventListener("mouseup", stop);
  }, []);

  // Keep the active cell in view while arrowing through a big sheet.
  useEffect(() => {
    const node = scroller.current;
    if (!node || !settled.current) {
      settled.current = true;
      return;
    }
    const top = HEAD_H + head.r * ROW_H;
    const frozenH = HEAD_H + frozen * ROW_H;
    if (head.r >= frozen) {
      if (top - ROW_H < node.scrollTop + frozenH - HEAD_H) node.scrollTop = top - frozenH;
      else if (top + ROW_H > node.scrollTop + node.clientHeight) {
        node.scrollTop = top + ROW_H - node.clientHeight;
      }
    }
    const left = GUTTER_W + (colLeft[head.c] ?? 0);
    const width = colLeft[head.c + 1] === undefined ? COL_W : colWidth(head.c);
    if (left < node.scrollLeft + GUTTER_W) node.scrollLeft = left - GUTTER_W;
    else if (left + width > node.scrollLeft + node.clientWidth) {
      node.scrollLeft = left + width - node.clientWidth;
    }
    // biome-ignore lint/correctness/useExhaustiveDependencies: colWidth reads colLeft
  }, [head, frozen, colLeft]);

  // --- ask superchat -----------------------------------------------------------------------------

  const label = rangeLabel(selection);
  const total = rangeCellCount(selection);
  const asksRange = picked && !!sheet;
  const whole = t`Whole sheet`;
  // The selected range as the engine excerpts it, else the whole file.
  const contextBlock = useCallback(async () => {
    const at = headVersion.current;
    if (asksRange) {
      const { block } = await source.tableRange({
        artifactId: at.id,
        sheet: sheetName,
        range: label,
      });
      return block;
    }
    return buildFileAsk({
      artifactId: at.id,
      name: artifact.name,
      version: at.version,
      label: whole,
    }).block;
  }, [asksRange, source, sheetName, label, artifact.name, whole]);

  // --- render -------------------------------------------------------------------------------

  const parts = (stage: ReactNode): ArtifactEditorParts => ({
    label: t`Sheet`,
    editable: canEdit,
    stage,
    navigator:
      table && table.sheets.length > 1 ? (
        <NavigatorFrame
          label={t`Sheets`}
          showLabel={t`Show sheets`}
          hideLabel={t`Hide sheets`}
          testId="sheet-list"
        >
          <ul
            aria-label={t`Sheets`}
            className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto px-2 pt-1 pb-3"
          >
            {table.sheets.map((entry, index) => (
              <li key={entry.name}>
                <button
                  type="button"
                  data-testid="sheet-tab"
                  aria-current={entry.name === sheetName ? "true" : undefined}
                  onClick={() => pickSheet(index)}
                  className={cn(
                    "flex h-7 w-full items-center truncate rounded-md px-2 text-start text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-ring",
                    entry.name === sheetName
                      ? "bg-accent font-medium text-foreground"
                      : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
                  )}
                >
                  <span className="truncate">{entry.name}</span>
                </button>
              </li>
            ))}
          </ul>
        </NavigatorFrame>
      ) : null,
    inspector: (
      <SelectionStats picked={asksRange} cells={cells(total)} {...rangeSum(cell, selection)} />
    ),
    history: editHistory,
    ask: {
      chips: asksRange
        ? [{ id: "selection", label: `${sheetName} · ${label}`, detail: cells(total) }]
        : [],
      onRemoveChip: () => setPicked(false),
      placeholder: asksRange
        ? t`Ask superchat to change this…`
        : t`Ask superchat to change this sheet…`,
      contextBlock,
    },
  });

  if (failed && !table) return parts(<ArtifactPreview artifact={artifact} bytes={bytes} />);
  if (!table || !sheet) return parts(<SheetNote>{t`Loading…`}</SheetNote>);

  const changedHere = (r: number, c: number) =>
    history.changed.has(cellKey(sheetName, r, c)) || overlay.has(cellKey(sheetName, r, c));
  let changedCount = overlay.size;
  for (const key of history.changed) {
    if (key.startsWith(`${sheetName}\u0000`) && !overlay.has(key)) changedCount += 1;
  }
  const first = Math.max(frozen, Math.floor(scrollTop / ROW_H) - OVERSCAN);
  const last = Math.min(rowCount, Math.ceil((scrollTop + viewH) / ROW_H) + OVERSCAN);
  const activeCell = cell(anchor.r, anchor.c);
  const barText = editing ? editing.draft : cellSource(activeCell);

  const rowNodes: ReactNode[] = [];
  const pushRow = (r: number) => {
    const isFrozen = r < frozen;
    rowNodes.push(
      <tr key={r} style={{ height: ROW_H }}>
        <th
          scope="row"
          className={`sticky start-0 z-10 border-e border-b border-border bg-muted text-center text-[11px] font-normal tabular-nums ${
            picked && selection.r0 <= r && r <= selection.r1
              ? "text-foreground"
              : "text-muted-foreground"
          }`}
          style={isFrozen ? { top: HEAD_H + r * ROW_H, zIndex: 15 } : undefined}
        >
          {r + 1}
        </th>
        {Array.from({ length: colCount }, (_, c) => {
          const value = cell(r, c);
          const shown = cellText(value);
          const isEditing = editing?.r === r && editing.c === c;
          const active = picked && anchor.r === r && anchor.c === c;
          const selected = picked && inRange(selection, r, c);
          return (
            <td
              key={c}
              data-r={r}
              data-c={c}
              data-selected={selected || undefined}
              data-marked={marked.has(`${r}:${c}`) || undefined}
              onDoubleClick={() => {
                if (editOn) setEditing({ r, c, draft: cellSource(value) });
              }}
              className={[
                "relative cursor-cell truncate border-e border-b border-border px-2.5 text-[13px]",
                isNumeric(value) ? "text-right tabular-nums" : "text-left",
                isFrozen
                  ? "sticky z-[5] bg-muted font-medium"
                  : marked.has(`${r}:${c}`)
                    ? "bg-accent underline decoration-primary/50 underline-offset-2"
                    : "bg-card",
                shown.muted ? "text-muted-foreground" : "text-foreground",
                selected && !active ? "bg-primary/10" : "",
                active ? "ring-2 ring-ring ring-inset" : "",
              ].join(" ")}
              style={isFrozen ? { top: HEAD_H + r * ROW_H } : undefined}
              title={isEditing || !shown.text ? undefined : shown.text}
            >
              {isEditing ? (
                <CellEditor
                  draft={editing.draft}
                  onChange={(draft) => setEditing({ r, c, draft })}
                  onDone={finishEdit}
                />
              ) : (
                shown.text
              )}
              {changedHere(r, c) ? (
                <span
                  role="img"
                  aria-label={t`Edited by you`}
                  className="absolute end-1 top-1 size-1.5 rounded-full bg-warning"
                />
              ) : null}
            </td>
          );
        })}
      </tr>,
    );
  };
  for (let r = 0; r < frozen; r += 1) pushRow(r);
  if (first > frozen) {
    rowNodes.push(<tr key="pad-top" aria-hidden style={{ height: (first - frozen) * ROW_H }} />);
  }
  for (let r = first; r < last; r += 1) pushRow(r);
  if (last < rowCount) {
    rowNodes.push(
      <tr key="pad-bottom" aria-hidden style={{ height: (rowCount - last) * ROW_H }} />,
    );
  }

  return parts(
    <div data-testid="sheet-view" className="flex h-full min-h-0 flex-col bg-background">
      <div className="mx-4 mt-2.5 flex shrink-0 items-center gap-2.5 rounded-lg border border-border bg-card px-2.5 py-1.5 text-[13px]">
        <b className="min-w-10 font-medium text-muted-foreground tabular-nums">
          {rangeLabel({ r0: anchor.r, c0: anchor.c, r1: anchor.r, c1: anchor.c })}
        </b>
        <span data-testid="formula-bar" className="min-w-0 flex-1 truncate font-mono text-[12.5px]">
          {barText}
        </span>
      </div>

      {/* biome-ignore lint/a11y/useSemanticElements: a spreadsheet grid is a focus-managed region */}
      <div
        ref={scroller}
        role="grid"
        tabIndex={0}
        aria-label={sheetName}
        aria-readonly={!editOn}
        onKeyDown={onGridKeyDown}
        onMouseDown={onMouseDown}
        onMouseMove={(event) => {
          if (!dragging.current) return;
          const pos = posOf(event);
          if (pos) setHead((now) => (now.r === pos.r && now.c === pos.c ? now : pos));
        }}
        onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
        className="relative mx-4 mt-2.5 min-h-0 flex-1 overflow-auto rounded-xl border border-border outline-none select-none focus-visible:border-ring/60"
      >
        <table
          className="border-separate border-spacing-0 table-fixed"
          style={{ width: GUTTER_W + (colLeft[colCount] ?? 0) }}
        >
          <colgroup>
            <col style={{ width: GUTTER_W }} />
            {Array.from({ length: colCount }, (_, c) => (
              <col key={c} style={{ width: colWidth(c) }} />
            ))}
          </colgroup>
          <thead>
            <tr style={{ height: HEAD_H }}>
              <th className="sticky start-0 top-0 z-30 border-e border-b border-border bg-muted" />
              {Array.from({ length: colCount }, (_, c) => (
                <th
                  key={c}
                  scope="col"
                  className={`sticky top-0 z-20 border-e border-b border-border text-[11px] font-normal ${
                    picked && selection.c0 <= c && c <= selection.c1
                      ? "bg-accent text-foreground"
                      : "bg-muted text-muted-foreground"
                  }`}
                >
                  {colLetter(c)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>{rowNodes}</tbody>
        </table>
      </div>

      {sheet.truncated ? (
        <p className="mx-4 mt-2 shrink-0 text-[12px] text-muted-foreground">
          {t`Showing the first ${sheet.rows.length} rows`}
        </p>
      ) : null}

      <div className="mx-4 my-3 flex shrink-0 flex-col gap-2">
        {error ? (
          <p role="alert" className="text-[13px] text-destructive">
            {error}
          </p>
        ) : null}
        {changedCount > 0 ? (
          <div
            data-testid="sheet-changes"
            className="flex flex-wrap items-center gap-2.5 rounded-[10px] border border-warning/40 bg-warning/10 px-3 py-2"
          >
            <span aria-hidden className="size-2 rounded-full bg-warning" />
            <span className="min-w-0 flex-1 basis-60 text-[13px]">
              {changedCount === 1
                ? t`You changed 1 cell on ${sheetName}. superchat sees this before its next reply.`
                : t`You changed ${changedCount} cells on ${sheetName}. superchat sees this before its next reply.`}
            </span>
          </div>
        ) : null}
      </div>
    </div>,
  );
}

/** CSV and XLSX files in the artifact editor. */
export const sheetEditorKind: ArtifactEditorKind = {
  id: "sheet",
  useEditor: useSheetEditor,
};

const XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
const NO_BYTES = new Uint8Array();

/** One sheet in the editor shell without the version machinery: the dev page and tests, with
 * their own table calls. */
export function SheetView({
  artifact,
  onEdited,
  source = defaultSource,
  botId = null,
}: {
  artifact: { id: string; name: string; version: number };
  onEdited?: (artifactId: string) => void;
  source?: SheetSource;
  /** The Muse that owns the file: the ask box asks it. */
  botId?: string | null;
}) {
  const file: Artifact = {
    ...artifact,
    mimeType: artifact.name.endsWith(".csv") ? "text/csv" : XLSX_MIME,
    botId,
    runId: null,
    description: null,
    size: 0,
    createdAt: "",
  };
  return (
    <SheetSourceContext.Provider value={source}>
      <ArtifactEditorView
        artifact={file}
        bytes={NO_BYTES}
        kind={sheetEditorKind}
        onEdited={onEdited}
      />
    </SheetSourceContext.Provider>
  );
}

/** The inspector: what the selected cells add up to. */
function SelectionStats({
  picked,
  cells,
  sum,
  numeric,
}: {
  picked: boolean;
  cells: string;
  sum: number;
  numeric: number;
}) {
  const { t } = useLingui();
  const stats: Array<[string, string]> = [
    [t`Sum`, numeric > 0 ? formatSum(sum) : "–"],
    [t`Count`, String(numeric)],
    [t`Average`, numeric > 0 ? formatSum(sum / numeric) : "–"],
  ];
  return (
    <aside
      data-testid="sheet-inspector"
      aria-label={t`Selection`}
      className="flex max-h-full w-[288px] flex-col gap-4 overflow-y-auto rounded-2xl bg-popover px-4 py-4 text-popover-foreground ring-1 ring-border"
    >
      {picked ? (
        <>
          <div className="text-[11px] tracking-[0.5px] text-muted-foreground uppercase">
            {t`Selection · ${cells}`}
          </div>
          {stats.map(([name, value]) => (
            <div key={name} className="flex flex-col gap-1.5">
              <span className="text-[12px] text-muted-foreground">{name}</span>
              <span className="text-[15px] font-semibold tabular-nums">{value}</span>
            </div>
          ))}
        </>
      ) : (
        <p className="text-[13px] text-muted-foreground">
          {t`Select cells to see their sum, count and average.`}
        </p>
      )}
    </aside>
  );
}

const EMPTY_SHEET = { rows: [] } as unknown as Parameters<typeof cellAt>[0];

const formatSum = (value: number): string =>
  value.toLocaleString("en-US", { maximumFractionDigits: 2 });

function SheetNote({ children }: { children: ReactNode }) {
  return (
    <div className="grid h-full place-items-center px-6 text-center text-[13.5px] text-muted-foreground">
      {children}
    </div>
  );
}

function CellEditor({
  draft,
  onChange,
  onDone,
}: {
  draft: string;
  onChange: (draft: string) => void;
  onDone: (save: boolean, move?: { dr: number; dc: number }) => void;
}) {
  const ref = useRef<HTMLInputElement>(null);
  const done = useRef(false);
  useEffect(() => {
    const input = ref.current;
    if (!input) return;
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }, []);
  const finish = (save: boolean, move?: { dr: number; dc: number }) => {
    if (done.current) return;
    done.current = true;
    onDone(save, move);
  };
  return (
    <input
      ref={ref}
      value={draft}
      spellCheck={false}
      aria-label="Cell"
      onChange={(event) => onChange(event.target.value)}
      onBlur={() => finish(true)}
      onKeyDown={(event) => {
        event.stopPropagation();
        if (event.key === "Enter") {
          event.preventDefault();
          finish(true, { dr: event.shiftKey ? -1 : 1, dc: 0 });
        } else if (event.key === "Tab") {
          event.preventDefault();
          finish(true, { dr: 0, dc: event.shiftKey ? -1 : 1 });
        } else if (event.key === "Escape") {
          event.preventDefault();
          finish(false);
        }
      }}
      className="absolute inset-0 w-full bg-card px-2.5 font-mono text-[12.5px] text-foreground outline-none ring-2 ring-ring ring-inset"
    />
  );
}

/** Which version this is, and what the person changed by hand since Superchat's last save. */
async function readHistory(
  source: SheetSource,
  id: string,
  current: ArtifactTable,
  versions: ReadonlyArray<{
    id: string;
    origin?: string;
    parentVersionId?: string | null;
  }>,
): Promise<History> {
  const byId = new Map(versions.map((version) => [version.id, version]));
  const here = byId.get(id);
  const info: History = { ...NO_HISTORY, newest: versions.length === 0 || versions[0]?.id === id };
  if (here?.origin !== "manual" || !here.parentVersionId) return info;
  // Walk back to the last version Superchat (or a restore) saved: that is what "changed" is against.
  let baseId = here.parentVersionId;
  for (let step = 0; step < 50; step += 1) {
    const entry = byId.get(baseId);
    if (entry?.origin !== "manual" || !entry.parentVersionId) break;
    baseId = entry.parentVersionId;
  }
  try {
    const base = await source.table({ artifactId: baseId });
    const changed = new Set(
      diffTables(base.sheets, current.sheets).map((d) => cellKey(d.sheet, d.row, d.col)),
    );
    return { ...info, changed };
  } catch {
    return info;
  }
}
