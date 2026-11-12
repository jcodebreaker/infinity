import { useLingui } from "@lingui/react/macro";
import type { BriefHomeLine } from "@superchat/contracts";
import { cn } from "@superchat/ui-web";
import { RotateCw } from "lucide-react";
import type { ReactNode } from "react";
import { useFileStudio } from "../../components/cards/context";
import type { LatestBriefState } from "./useLatestBrief";

/** Local midnight of *date*. */
function dayStart(date: Date): number {
  const copy = new Date(date);
  copy.setHours(0, 0, 0, 0);
  return copy.getTime();
}

/**
 * The newest morning brief on Home, under the topic tiles (ADR 0037, amended): "This morning ·
 * 7:34" over one line per topic, each with the topic's tag and a sentence whose second half is
 * bold (what is ready, or what needs the person). The heading opens the brief's page; Refresh
 * now makes a new version in the background. Nothing renders until there is a first brief.
 */
export function MorningPanel({
  latest,
  tag,
  className,
}: {
  latest: LatestBriefState;
  /** The topic's tag for a line (the caller knows the topics and their colours). */
  tag: (line: BriefHomeLine) => ReactNode;
  className?: string;
}) {
  const { t, i18n } = useLingui();
  const studio = useFileStudio();
  const { brief, refreshing, failed, refresh } = latest;
  const page = brief?.page;
  if (!page) return null;
  const saved = new Date(page.savedAt);
  const valid = !Number.isNaN(saved.getTime());
  const time = valid ? i18n.date(saved, { hour: "numeric", minute: "2-digit" }) : "";
  const ago = valid ? Math.round((dayStart(new Date()) - dayStart(saved)) / 86_400_000) : 99;
  const date = valid ? i18n.date(saved, { weekday: "short", day: "numeric", month: "short" }) : "";
  const heading = refreshing
    ? t`Making a new one…`
    : ago === 0
      ? saved.getHours() < 12
        ? t`This morning · ${time}`
        : t`Today · ${time}`
      : ago === 1
        ? t`Yesterday · ${time}`
        : t`${date} · ${time}`;
  const lines = page.home ?? [];
  return (
    <article
      data-testid="brief-card"
      aria-label={t`Your brief`}
      className={cn("sc-morning sc-glass mt-[22px] rounded-[22px] px-[22px] py-[18px]", className)}
    >
      <header className="mb-2.5 flex flex-wrap items-center justify-between gap-x-3.5 gap-y-1">
        <button
          type="button"
          onClick={() => studio?.open(page.artifactId, page.title)}
          title={t`Open your brief`}
          className="rounded-md text-[15px] leading-tight font-semibold tracking-[-0.01em] hover:underline focus-visible:outline-2 focus-visible:outline-ring"
        >
          {heading}
        </button>
        {brief?.taskId ? (
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={refreshing}
            aria-label={t`Refresh now`}
            title={t`Refresh now`}
            className="-me-2 grid size-8 place-items-center rounded-lg text-ink-2 hover:bg-foreground/10 focus-visible:outline-2 focus-visible:outline-ring disabled:opacity-60"
          >
            <RotateCw
              size={15}
              strokeWidth={1.9}
              aria-hidden="true"
              className={refreshing ? "motion-safe:animate-spin" : undefined}
            />
          </button>
        ) : null}
      </header>
      <ul className="m-0 grid list-none gap-2.5 p-0">
        {lines.length ? (
          lines.map((line) => (
            <li
              key={`${line.topicKey}:${line.text}`}
              className="flex items-baseline gap-2.5 text-[14.5px] leading-[1.45]"
            >
              {tag(line)}
              <span dir="auto">
                {line.text}
                {line.text && line.bold ? " " : null}
                {line.bold ? <b>{line.bold}</b> : null}
              </span>
            </li>
          ))
        ) : (
          <li className="text-[14.5px] leading-[1.45]" dir="auto">
            {page.line || page.title}
          </li>
        )}
      </ul>
      {failed ? (
        <p className="mt-2 text-[12.5px] text-destructive">{t`Could not start it.`}</p>
      ) : null}
    </article>
  );
}
