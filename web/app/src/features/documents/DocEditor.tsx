import { useLingui } from "@lingui/react/macro";
import { ChatMarkdown } from "@superchat/chat-ui/web";
import { cn, NativeSelect, NativeSelectOption } from "@superchat/ui-web";
import {
  type KeyboardEvent as ReactKeyboardEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ArtifactEditorArgs, ArtifactEditorKind, ArtifactEditorParts } from "../artifacts";
import { artifactKey } from "../artifacts/editor/editor-state";
import { EditNoticeBanner } from "../artifacts/editor/html/StylePanel";
import { useHtmlEditor } from "../artifacts/editor/html/useHtmlEditor";
import { buildBlockAsk, buildFileAsk } from "../artifacts/editor/html-ask";
import { NavigatorFrame } from "../artifacts/editor/NavigatorFrame";
import {
  type BlockStyle,
  type BlockType,
  blockPlace,
  blockStyle,
  type MdBlock,
  replaceBlock,
  restyleBlock,
  splitBlocks,
} from "./markdown-blocks";

const NO_POST = () => {};
/** The words the engine's version note uses (superchat reads them, so not translated). */
const NOTE_WORDS: Record<BlockType, string> = {
  heading: "heading",
  paragraph: "paragraph",
  list: "list",
  quote: "quote",
  code: "code block",
  table: "table",
  rule: "divider",
};

/** The stage around the page, and the page: a document's width, serif reading type. */
const STAGE = "@container h-full overflow-y-auto bg-muted/40 px-4 py-5";
const PAGE =
  "mx-auto w-full max-w-[680px] rounded-lg border border-border bg-card px-6 py-8 font-serif " +
  "text-[15px] leading-[1.6] text-foreground @[560px]:px-12 @[560px]:py-9";
/** Room above a block: more before a heading. */
const gapBefore = (block: MdBlock, index: number) =>
  index === 0 ? "" : block.type === "heading" ? "mt-7" : "mt-3.5";

const lowerFirst = (text: string) => text.charAt(0).toLocaleLowerCase() + text.slice(1);

/**
 * A markdown document in the artifact editor: the rendered document on a page-width column, its
 * headings as the outline. Behind the shell's Edit a click selects a block (the ask box then asks
 * about it), a double-click or Enter edits its markdown in place, and each committed block saves
 * the whole new text as a version through the same edit history as pages, so Undo saves the
 * text before it back.
 */
function useDocEditor({
  artifact,
  bytes,
  editing,
  onEdited,
  focusAsk,
  locked,
}: ArtifactEditorArgs): ArtifactEditorParts {
  const { t } = useLingui();
  const source = useMemo(() => new TextDecoder("utf-8").decode(bytes), [bytes]);
  const editor = useHtmlEditor({
    fileKey: artifactKey(artifact),
    active: editing,
    artifactId: artifact.id,
    version: artifact.version,
    html: source,
    onEdited,
    post: NO_POST,
    locked,
  });
  // A saved edit shows at once; the version it becomes replaces it (or a failed save drops it).
  const [pending, setPending] = useState<string | null>(null);
  useEffect(() => setPending(null), [source]);
  const failed = editor.notice !== null;
  useEffect(() => {
    if (failed) setPending(null);
  }, [failed]);
  const shown = pending ?? source;
  const blocks = useMemo(() => splitBlocks(shown), [shown]);
  const [picked, setPicked] = useState<number | null>(null);
  const [draft, setDraft] = useState<{ index: number; text: string } | null>(null);
  const blockNodes = useRef<Array<HTMLElement | null>>([]);
  const page = useRef<HTMLDivElement>(null);

  const selected = editing && picked !== null && picked < blocks.length ? picked : null;
  // Leaving edit mode ends the selection and drops a block being typed into.
  useEffect(() => {
    if (editing) return;
    setPicked(null);
    setDraft(null);
  }, [editing]);

  const words: Record<BlockType, string> = {
    heading: t`Heading`,
    paragraph: t`Paragraph`,
    list: t`List`,
    quote: t`Quote`,
    code: t`Code`,
    table: t`Table`,
    rule: t`Divider`,
  };
  /** "Paragraph 2", "Heading". */
  const nameOf = (index: number) => {
    const block = blocks[index] as MdBlock;
    return block.type === "heading"
      ? words.heading
      : `${words[block.type]} ${blockPlace(blocks, index).n}`;
  };
  /** "Goals · paragraph 2". */
  const chipOf = (index: number) => {
    const { section } = blockPlace(blocks, index);
    return section ? `${section} · ${lowerFirst(nameOf(index))}` : nameOf(index);
  };

  const save = (index: number, text: string) => {
    const block = blocks[index];
    if (!block || text === block.source) return;
    const next = replaceBlock(shown, block, text);
    if (!next.trim()) return;
    const { section, n } = blockPlace(blocks, index);
    const what = block.type === "heading" ? "heading" : `${NOTE_WORDS[block.type]} ${n}`;
    setPending(next);
    void editor.saveSource(next, `edited ${what}${section ? ` under "${section}"` : ""}`);
  };

  const finishDraft = (commit: boolean) => {
    if (!draft) return;
    setDraft(null);
    if (commit) save(draft.index, draft.text);
    window.requestAnimationFrame(() => page.current?.focus({ preventScroll: true }));
  };

  const select = (index: number) => {
    setPicked(index);
    blockNodes.current[index]?.scrollIntoView?.({ block: "nearest" });
  };

  function onKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (!editing || draft || event.target !== event.currentTarget) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      select(Math.max(0, Math.min(blocks.length - 1, (selected ?? -step) + step)));
    } else if (event.key === "Enter" && selected !== null) {
      event.preventDefault();
      setDraft({ index: selected, text: (blocks[selected] as MdBlock).source });
    } else if (event.key === "Escape" && selected !== null) {
      event.preventDefault();
      event.stopPropagation();
      setPicked(null);
    }
  }

  const headings = blocks
    .map((block, index) => ({ block, index }))
    .filter(({ block }) => block.type === "heading" && (block.level ?? 9) <= 3);
  const topLevel = Math.min(...headings.map(({ block }) => block.level ?? 1));
  const currentHeading =
    selected === null ? undefined : headings.findLast((h) => h.index <= selected)?.index;

  const goTo = (index: number) => {
    blockNodes.current[index]?.scrollIntoView?.({ block: "start", behavior: "smooth" });
    if (editing) setPicked(index);
  };

  const whole = t`Whole document`;
  const at = editor.head;
  const pickedBlock = selected === null ? null : (blocks[selected] as MdBlock);
  const chip = selected === null ? null : chipOf(selected);
  const contextBlock = useCallback(() => {
    const head = at.current;
    const file = { artifactId: head.id, name: artifact.name, version: head.version };
    if (!pickedBlock || !chip) return buildFileAsk({ ...file, label: whole }).block;
    return buildBlockAsk({
      ...file,
      label: chip,
      lines: [pickedBlock.line, pickedBlock.lastLine],
      source: pickedBlock.source,
    }).block;
  }, [at, artifact.name, pickedBlock, chip, whole]);

  const style = pickedBlock ? blockStyle(pickedBlock) : null;
  const styles: Array<[BlockStyle, string]> = [
    ["paragraph", t`Paragraph`],
    ["h1", t`Heading 1`],
    ["h2", t`Heading 2`],
    ["h3", t`Heading 3`],
    ["quote", t`Quote`],
    ["list", t`List`],
  ];

  const stage = (
    <div data-testid="doc-stage" className={STAGE}>
      {/* biome-ignore lint/a11y/noStaticElementInteractions: the page takes the arrow keys in edit mode */}
      <div
        ref={page}
        tabIndex={editing ? 0 : undefined}
        onKeyDown={onKeyDown}
        className={cn(PAGE, "outline-none")}
      >
        {blocks.map((block, index) => {
          const isSelected = index === selected;
          const isDraft = draft?.index === index;
          return (
            // biome-ignore lint/a11y/noStaticElementInteractions: blocks are picked by click; the page has the keys
            // biome-ignore lint/a11y/useKeyWithClickEvents: the page has the keys
            <div
              key={`${block.start}:${block.type}`}
              ref={(node) => {
                blockNodes.current[index] = node;
              }}
              data-testid="doc-block"
              data-selected={isSelected || undefined}
              onClick={() => {
                if (!editing || isDraft) return;
                setPicked(index);
                page.current?.focus({ preventScroll: true });
              }}
              onDoubleClick={() => {
                if (editing && !isDraft) setDraft({ index, text: block.source });
              }}
              className={cn(
                "relative scroll-mt-8 rounded-[2px]",
                gapBefore(block, index),
                isDraft
                  ? ""
                  : isSelected
                    ? "outline-2 outline-offset-4 outline-ring"
                    : editing
                      ? "hover:outline-1 hover:outline-offset-4 hover:outline-border"
                      : "",
              )}
            >
              {isSelected && !isDraft ? (
                <span className="absolute -top-[26px] -start-1 flex h-[18px] items-center rounded-[5px] bg-ring px-[7px] font-sans text-[10px] text-primary-foreground">
                  {nameOf(index)}
                </span>
              ) : null}
              {isDraft ? (
                <BlockSource
                  text={draft.text}
                  label={nameOf(index)}
                  onChange={(text) => setDraft({ index, text })}
                  onDone={finishDraft}
                />
              ) : (
                <ChatMarkdown>{block.source}</ChatMarkdown>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );

  return {
    label: t`Document`,
    editable: true,
    stage,
    navigator: headings.length ? (
      <NavigatorFrame
        label={t`Outline`}
        showLabel={t`Show outline`}
        hideLabel={t`Hide outline`}
        testId="doc-outline"
      >
        <ul
          aria-label={t`Outline`}
          className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto px-2 pt-1 pb-3"
        >
          {headings.map(({ block, index }) => {
            const depth = (block.level ?? 1) - topLevel;
            const current = currentHeading === index;
            return (
              <li key={`${block.start}`}>
                <button
                  type="button"
                  data-testid="doc-outline-item"
                  aria-current={current ? "true" : undefined}
                  title={block.text}
                  onClick={() => goTo(index)}
                  className={cn(
                    "flex h-7 w-full items-center truncate rounded-md px-2 text-start text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-ring",
                    depth === 1 ? "ps-5" : depth >= 2 ? "ps-8" : "",
                    current
                      ? "bg-accent font-medium text-foreground"
                      : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
                  )}
                >
                  <span className="truncate">{block.text}</span>
                </button>
              </li>
            );
          })}
        </ul>
      </NavigatorFrame>
    ) : null,
    inspector: (
      <aside
        data-testid="doc-inspector"
        aria-label={t`Selection`}
        className="flex max-h-full w-[288px] flex-col gap-4 overflow-y-auto rounded-2xl bg-popover px-4 py-4 text-popover-foreground ring-1 ring-border"
      >
        {pickedBlock && selected !== null ? (
          <>
            <div className="text-[11px] tracking-[0.5px] text-muted-foreground uppercase">
              {t`Selection · ${words[pickedBlock.type]}`}
            </div>
            {style ? (
              <div className="flex flex-col gap-1.5">
                <span className="text-[12px] text-muted-foreground">{t`Block`}</span>
                <NativeSelect
                  size="sm"
                  className="w-full"
                  aria-label={t`Block`}
                  value={style}
                  onChange={(event) =>
                    save(selected, restyleBlock(pickedBlock, event.target.value as BlockStyle))
                  }
                >
                  {styles.map(([value, name]) => (
                    <NativeSelectOption key={value} value={value}>
                      {name}
                    </NativeSelectOption>
                  ))}
                </NativeSelect>
              </div>
            ) : null}
            <p className="text-[12px] leading-normal text-muted-foreground">
              {t`Double-click or press Enter to edit its text.`}
            </p>
          </>
        ) : (
          <p className="text-[13px] text-muted-foreground">{t`Select a block to change it.`}</p>
        )}
      </aside>
    ),
    notice:
      editing && editor.notice ? (
        <EditNoticeBanner
          notice={editor.notice}
          canAsk={!!artifact.botId && selected !== null}
          onAsk={focusAsk}
          onDismissNotice={editor.dismissNotice}
        />
      ) : null,
    history: editor.history,
    ask: {
      chips: chip ? [{ id: "block", label: chip }] : [],
      onRemoveChip: () => setPicked(null),
      placeholder: chip
        ? t`Ask superchat to change this…`
        : t`Ask superchat to change this document…`,
      contextBlock,
    },
  };
}

/** A block's markdown, typed into in place: grows with its text; Escape cancels, ⌘Enter or
 * leaving it saves. */
function BlockSource({
  text,
  label,
  onChange,
  onDone,
}: {
  text: string;
  label: string;
  onChange: (text: string) => void;
  onDone: (commit: boolean) => void;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const done = useRef(false);
  useEffect(() => {
    const input = ref.current;
    if (!input) return;
    input.focus({ preventScroll: true });
    input.setSelectionRange(input.value.length, input.value.length);
  }, []);
  useLayoutEffect(() => {
    const input = ref.current;
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
  }, [text]);
  const finish = (commit: boolean) => {
    if (done.current) return;
    done.current = true;
    onDone(commit);
  };
  return (
    <textarea
      ref={ref}
      value={text}
      aria-label={label}
      spellCheck
      rows={1}
      onChange={(event) => onChange(event.target.value)}
      onBlur={() => finish(true)}
      onKeyDown={(event) => {
        event.stopPropagation();
        if (event.key === "Escape") {
          event.preventDefault();
          finish(false);
        } else if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
          event.preventDefault();
          finish(true);
        }
      }}
      className="block w-full resize-none overflow-hidden rounded-md bg-muted/60 px-2 py-1.5 font-mono text-[13px] leading-relaxed text-foreground outline-2 outline-offset-2 outline-ring"
    />
  );
}

/** Markdown files in the artifact editor. */
export const docEditorKind: ArtifactEditorKind = {
  id: "doc",
  useEditor: useDocEditor,
};

/** A markdown file read-only (a host file's preview): the same page, no editing. */
export function DocReader({ bytes }: { bytes: Uint8Array }) {
  const blocks = useMemo(() => splitBlocks(new TextDecoder("utf-8").decode(bytes)), [bytes]);
  return (
    <div className={STAGE}>
      <article className={PAGE}>
        {blocks.map((block, index) => (
          <div key={`${block.start}:${block.type}`} className={gapBefore(block, index)}>
            <ChatMarkdown>{block.source}</ChatMarkdown>
          </div>
        ))}
      </article>
    </div>
  );
}
