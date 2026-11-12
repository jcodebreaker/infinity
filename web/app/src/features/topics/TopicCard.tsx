import { plural } from "@lingui/core/macro";
import { useLingui } from "@lingui/react/macro";
import type { ReplyCardDataOf } from "@superchat/contracts";
import { Button, cn } from "@superchat/ui-web";
import { ChevronRight, FileText } from "lucide-react";
import { useMemo, useState } from "react";
import { KIND_DOT, KindTag } from "./TopicParts";
import { type PickRow, pickRows, workAllText } from "./topicLook";
import { useTopicWords } from "./useTopics";

type TopicCardData = ReplyCardDataOf<"topic">;

/** The hero's contour lines, bent into one peak near its bottom-right corner (drawn once). */
function heroContours(): string[] {
  const width = 640;
  const height = 90;
  const [px, py, spread] = [width * 0.82, height * 0.95, 130];
  return Array.from({ length: 12 }, (_, row) => {
    const i = row - 2;
    const points: string[] = [];
    for (let x = 0; x <= width; x += 8) {
      const y0 = ((i + 0.5) * height) / 10;
      const dx = x - px;
      const dy = y0 - py;
      const y =
        y0 +
        Math.sin(x * 0.01 + i) * 2.5 -
        44 * Math.exp(-(dx * dx + dy * dy * 2) / (2 * spread * spread));
      points.push(`${x},${y.toFixed(1)}`);
    }
    return `M${points.join(" L")}`;
  });
}
const HERO_CONTOURS = heroContours();

/** A row's colour by kind (a starter no line carries is tinted): its dot and due words, and the
 * Next row's wash. */
const ROW_TONE: Record<NonNullable<PickRow["kind"]> | "starter", { ink: string; wash: string }> = {
  Promised: { ink: "text-tint-3", wash: "bg-tint-3/10" },
  Open: { ink: "text-warn", wash: "bg-warn/10" },
  starter: { ink: "text-tint", wash: "bg-tint/10" },
};

/**
 * The pick-up card (ADR 0035, amended 2026-10-25) the engine adds to the Conversation when the
 * person picks up a topic: where we left off, the one thing to do next, what else is open, and
 * what we covered folded away. Each row's button fills the composer with its starter for the
 * person to edit or send; it is never sent on its own. "Work through all" (two rows or more) is
 * the one button that sends: the click is the ask. A draft the Lookout prepared since the topic
 * last moved leads "Also open", with "Review the draft".
 */
export function TopicCard({
  data,
  onDraft,
  onSend,
  onOpenFile,
}: {
  data: TopicCardData;
  /** Puts a starter in the composer; without it the rows carry no buttons. */
  onDraft?: (text: string) => void;
  /** Sends the person's message; without it there is no "Work through all". */
  onSend?: (text: string) => void;
  /** Opens a file (the prepared draft) in the File Studio. */
  onOpenFile?: (artifactId: string, title: string) => void;
}) {
  const { t } = useLingui();
  const words = useTopicWords();
  const now = useMemo(() => new Date(), []);
  const { next, also } = useMemo(() => pickRows(data, now), [data, now]);
  const rows = next ? [next, ...also] : also;
  const draft = data.draft;
  // Sent from this card: it is history once the work starts.
  const [working, setWorking] = useState(false);
  const files = data.sources?.files ?? 0;
  const coveredSummary = [
    t`What we covered`,
    plural(data.covered.length, { one: "# step", other: "# steps" }),
    files ? plural(files, { one: "# file", other: "# files" }) : null,
  ]
    .filter(Boolean)
    .join(" · ");
  const verb = (row: PickRow) => row.verb ?? t`Start`;
  // Each part rises in turn, top to bottom.
  let step = 0;
  const rise = () => ({ animationDelay: `${250 + step++ * 110}ms` });
  const reveal =
    "motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-bottom-2 motion-safe:fill-mode-both motion-safe:animation-duration-600";
  const label = "m-0 text-[11.5px] font-semibold tracking-[0.06em] text-ink-2 uppercase";
  return (
    <article
      data-testid="topic-card-picked"
      aria-label={t`Where we left off on ${data.title}`}
      className={cn(
        "w-[min(46rem,calc(100vw-3rem))] max-w-full overflow-hidden rounded-[22px] border border-line bg-card pb-[18px] text-card-foreground shadow-sm",
        reveal,
      )}
    >
      <div className="relative overflow-hidden px-5 pt-[18px] pb-1">
        <svg
          aria-hidden="true"
          viewBox="0 0 640 90"
          preserveAspectRatio="none"
          className="pointer-events-none absolute inset-0 size-full text-tint"
        >
          <g fill="none" stroke="currentColor" strokeWidth="1.1">
            {HERO_CONTOURS.map((d, index) => (
              <path
                key={d}
                d={d}
                strokeOpacity={0.04 + 0.12 * Math.max(0, 1 - Math.abs(index - 6) / 6)}
              />
            ))}
          </g>
        </svg>
        <div className="relative flex justify-between gap-3 text-[11.5px] font-semibold tracking-[0.06em] text-tint-ink uppercase">
          {t`Where we left off`}
          <span className="font-normal tracking-normal text-ink-2 normal-case tabular-nums">
            {words.when(data.last_touched_at, now)}
          </span>
        </div>
        <h3
          className="relative mt-1 text-[22px] font-semibold tracking-[-0.02em] text-balance"
          dir="auto"
        >
          {data.title}
        </h3>
      </div>
      {data.left_off ? (
        <p className="mx-5 mt-2 max-w-[60ch] text-[14.5px] leading-[1.5]" dir="auto">
          {data.left_off}
        </p>
      ) : null}
      {next ? (
        <div
          style={rise()}
          className={cn(
            "mx-5 mt-4 flex flex-wrap items-center gap-3.5 rounded-2xl py-3.5 ps-4 pe-3.5",
            ROW_TONE[next.kind ?? "starter"].wash,
            reveal,
          )}
        >
          <div className="min-w-0 flex-[1_1_16rem]">
            {next.kind ? (
              <div
                className={cn(
                  "inline-flex items-center gap-1.5 text-[12px] font-semibold",
                  ROW_TONE[next.kind].ink,
                )}
              >
                <span aria-hidden="true" className="size-[7px] rounded-full bg-current" />
                {[words.kinds[next.kind], next.due ? words.dueWords(next.due, now) : ""]
                  .filter(Boolean)
                  .join(" · ")}
              </div>
            ) : null}
            <div className="mt-0.5 text-[15px] leading-[1.4] font-semibold" dir="auto">
              {next.text}
            </div>
          </div>
          {onDraft ? (
            <Button
              type="button"
              onClick={() => onDraft(next.starter ?? next.text)}
              className="h-auto rounded-full px-4 py-[9px] text-[13.5px] font-semibold motion-safe:hover:-translate-y-px"
            >
              {verb(next)}
            </Button>
          ) : null}
        </div>
      ) : null}
      {also.length || draft ? (
        <div style={rise()} className={cn("mx-5 mt-[18px]", reveal)}>
          <h4 className={cn(label, "mb-2")}>{t`Also open`}</h4>
          <ul className="m-0 list-none border-t border-line p-0">
            {draft ? (
              <li className="flex items-center gap-3 border-b border-line py-2.5 text-[14px] leading-[1.4]">
                <FileText aria-hidden="true" size={14} className="shrink-0 text-tint" />
                <span className="min-w-0 flex-1" dir="auto">
                  {t`A draft is ready for you`}
                </span>
                {onOpenFile ? (
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={() => onOpenFile(draft.artifact_id, draft.title || data.title)}
                    className="h-auto rounded-full px-2.5 py-1.5 text-[13.5px] font-semibold text-tint-ink hover:bg-tint/10 hover:text-tint-ink"
                  >
                    {t`Review the draft`}
                  </Button>
                ) : null}
              </li>
            ) : null}
            {also.map((row) => (
              <li
                key={row.key}
                className="flex items-center gap-3 border-b border-line py-2.5 text-[14px] leading-[1.4]"
              >
                <span
                  aria-hidden="true"
                  className={cn(
                    "size-[7px] shrink-0 rounded-full bg-current",
                    ROW_TONE[row.kind ?? "starter"].ink,
                  )}
                />
                <span className="min-w-0 flex-1" dir="auto">
                  {row.text}
                </span>
                {onDraft ? (
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={() => onDraft(row.starter ?? row.text)}
                    className="h-auto rounded-full px-2.5 py-1.5 text-[13.5px] font-semibold text-tint-ink hover:bg-tint/10 hover:text-tint-ink"
                  >
                    {verb(row)}
                  </Button>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {onSend && rows.length >= 2 ? (
        <div style={rise()} className={cn("mx-5 mt-3.5 flex flex-wrap items-center gap-3", reveal)}>
          <Button
            type="button"
            variant="ghost"
            disabled={working}
            onClick={() => {
              setWorking(true);
              onSend(workAllText(t`Work through everything still open on ${data.title}`, rows));
            }}
            className="h-auto rounded-full border border-tint/35 bg-tint/10 px-4 py-[9px] text-[13.5px] font-semibold text-tint-ink hover:bg-tint/20 hover:text-tint-ink disabled:opacity-55"
          >
            <span aria-hidden="true">✦</span>
            {working ? t`Working through ${rows.length}` : t`Work through all ${rows.length}`}
          </Button>
          <span className="min-w-0 flex-[1_1_14rem] text-[12.5px] text-ink-2">
            {t`Does what it can, drafts what needs your yes, and asks you the decisions at the end.`}
          </span>
        </div>
      ) : null}
      {data.covered.length ? (
        <details style={rise()} className={cn("group mx-5 mt-2.5", reveal)}>
          <summary className="inline-flex cursor-pointer list-none items-center gap-2 py-1 text-[13px] text-ink-2 hover:text-foreground [&::-webkit-details-marker]:hidden">
            <ChevronRight
              aria-hidden="true"
              size={14}
              strokeWidth={2}
              className="transition-transform group-open:rotate-90"
            />
            {coveredSummary}
          </summary>
          <ol className="relative m-0 mt-2.5 flex list-none flex-col gap-3.5 p-0 before:absolute before:start-[5px] before:top-1.5 before:bottom-1.5 before:w-[1.5px] before:bg-line">
            {data.covered.map((line) => (
              <li
                key={`${line.date}:${line.text}`}
                className="relative flex flex-col gap-[3px] ps-[22px]"
              >
                <span
                  aria-hidden="true"
                  className={cn(
                    "absolute start-0 top-1 size-3 rounded-full border-2 bg-card",
                    KIND_DOT[line.kind],
                  )}
                />
                <span className="flex items-center gap-2 text-[11.5px] text-ink-2 tabular-nums">
                  {words.coveredDay(line.date, now)}
                  <KindTag kind={line.kind} />
                </span>
                <span className="text-[13.5px] leading-[1.45]" dir="auto">
                  {line.text}
                </span>
                {line.file ? (
                  <span className="mt-[3px] inline-flex items-center gap-1.5 self-start rounded-[9px] border border-line bg-muted/40 px-[9px] py-[5px] text-[12.5px]">
                    <FileText aria-hidden="true" size={14} className="text-tint" />
                    <span dir="auto">{line.file}</span>
                  </span>
                ) : null}
              </li>
            ))}
          </ol>
        </details>
      ) : null}
    </article>
  );
}
