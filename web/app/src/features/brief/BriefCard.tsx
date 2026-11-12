import { useLingui } from "@lingui/react/macro";
import { cn } from "@superchat/ui-web";
import { RotateCw } from "lucide-react";
import { useFileStudio } from "../../components/cards/context";
import { type BriefWire, type LatestBriefState, useLatestBrief } from "./useLatestBrief";

/** "7:31" today, "Thu 7:31" this week, else the date. */
function savedWhen(iso: string, locale: string | undefined, now = new Date()): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const time = date.toLocaleTimeString(locale || "en", { hour: "numeric", minute: "2-digit" });
  if (date.toDateString() === now.toDateString()) return time;
  if (now.getTime() - date.getTime() < 6 * 86_400_000) {
    return `${date.toLocaleDateString(locale || "en", { weekday: "short" })} ${time}`;
  }
  return date.toLocaleDateString(locale || "en", { month: "short", day: "numeric" });
}

/**
 * The newest morning brief as a card (ADR 0037): "Brief" with its time, the page's title and the
 * first thing that needs the person. It opens the page in the File Studio; Refresh now makes a new
 * version in the background. Nothing renders until there is a first brief.
 */
export function BriefCard({
  botId,
  className,
  wire,
}: {
  botId: string;
  className?: string;
  /** Tests and previews pass their own; the app uses the live RPC. */
  wire?: BriefWire;
}) {
  return <BriefCardView latest={useLatestBrief(botId, wire)} className={className} />;
}

/** The card over a brief read by the caller's `useLatestBrief` (Home asks whether one exists). */
export function BriefCardView({
  latest,
  className,
}: {
  latest: LatestBriefState;
  className?: string;
}) {
  const { t, i18n } = useLingui();
  const studio = useFileStudio();
  const { brief, refreshing, failed, refresh } = latest;
  const page = brief?.page;
  if (!page) return null;
  return (
    <article
      data-testid="brief-card"
      className={cn(
        "sc-glass relative flex w-full flex-col gap-2 rounded-[20px] p-4 text-start md:w-[300px]",
        className,
      )}
    >
      <div className="flex items-center gap-2 pe-9 text-[12.5px] text-ink-2">
        <span className="rounded-full bg-foreground/10 px-2 py-0.5 font-medium text-foreground">
          {t`Brief`}
        </span>
        <span>{refreshing ? t`Making a new one…` : savedWhen(page.savedAt, i18n.locale)}</span>
      </div>
      <button
        type="button"
        onClick={() => studio?.open(page.artifactId, page.title)}
        className="flex flex-col gap-1 rounded-lg text-start after:absolute after:inset-0 after:rounded-[20px] focus-visible:outline-2 focus-visible:outline-ring"
      >
        <span className="text-[15px] font-medium text-foreground" dir="auto">
          {page.title}
        </span>
        {page.line ? (
          <span className="line-clamp-2 text-[13.5px] leading-snug text-ink-2" dir="auto">
            {page.line}
          </span>
        ) : null}
      </button>
      {failed ? (
        <span className="text-[12.5px] text-destructive">{t`Could not start it.`}</span>
      ) : null}
      {brief?.taskId ? (
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={refreshing}
          aria-label={t`Refresh now`}
          title={t`Refresh now`}
          className="absolute end-2.5 top-2.5 z-10 grid size-9 place-items-center rounded-lg text-ink-2 hover:bg-foreground/10 focus-visible:outline-2 focus-visible:outline-ring disabled:opacity-60"
        >
          <RotateCw
            size={16}
            strokeWidth={1.9}
            aria-hidden="true"
            className={refreshing ? "animate-spin" : undefined}
          />
        </button>
      ) : null}
    </article>
  );
}
