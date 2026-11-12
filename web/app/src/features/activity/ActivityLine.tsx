import { useLingui } from "@lingui/react/macro";
import type { Activity, ActivitySource } from "@superchat/contracts";
import { cn } from "@superchat/ui-web";
import { CircleAlert, CircleSlash } from "lucide-react";
import type { ReactNode } from "react";
import { ChevronGlyph } from "../../components/chrome/SuperchatGlyphs";
import { SuperchatTile, type TileTone } from "../../components/chrome/SuperchatTile";
import { ACTIVITY_SOURCE_ICON } from "../../components/chrome/toolIcons";

/** Live work: a small accent dot with a slow opacity pulse (still under reduced motion). The
 * orb itself lives in one place only (components/ai/orb/placement.tsx). */
export function WorkingDot() {
  return (
    <span
      aria-hidden="true"
      className="size-[7px] shrink-0 rounded-full bg-tint motion-safe:animate-[rkPulse_1.6s_ease-in-out_infinite]"
    />
  );
}

/** Where a settled piece of work came from, as a tile color. */
const SOURCE_TONE: Record<ActivitySource, TileTone> = {
  turn: "blue",
  background: "indigo",
  housekeeping: "yellow",
  scheduled: "orange",
  studio: "teal",
};

/** The lead of a work row: a small live orb while it works, else a tile for its source (a
 * red alert when it failed, gray when cancelled), or a plain dot before the feed lists it. */
function WorkLead({
  status,
  source,
  size,
}: {
  status?: Activity["status"];
  source?: ActivitySource;
  size: number;
}) {
  if (status === "in_progress") {
    return (
      <span aria-hidden="true" className="grid place-items-center" style={{ width: size }}>
        <WorkingDot />
      </span>
    );
  }
  if (!status) {
    return (
      <span aria-hidden="true" className="grid place-items-center" style={{ width: size }}>
        <span className="size-1.5 rounded-full bg-ink-3/60" />
      </span>
    );
  }
  if (status === "failed") {
    return (
      <SuperchatTile tone="red" size={size}>
        <CircleAlert strokeWidth={2.4} />
      </SuperchatTile>
    );
  }
  if (status === "cancelled") {
    return (
      <SuperchatTile tone="gray" size={size}>
        <CircleSlash strokeWidth={2.4} />
      </SuperchatTile>
    );
  }
  const Icon = ACTIVITY_SOURCE_ICON[source ?? "turn"];
  return (
    <SuperchatTile tone={SOURCE_TONE[source ?? "turn"]} size={size}>
      <Icon strokeWidth={2.4} />
    </SuperchatTile>
  );
}

/**
 * One piece of work (the "Working" line), in two shapes:
 * - `list` (the inspector): an iOS grouped-list row, 52px, a pulsing dot or a colored source tile
 *   on the left, the title over "Now · <live step>" or the outcome, a mono figure (elapsed
 *   while working, else the clock) and a chevron. Rows sit in a `.superchat-group`.
 * - `inline` (under a message that started a Helper): a soft rounded row with a tiny orb, the
 *   title, the step and a timer (a small pulsing dot while it works).
 */
export function ActivityLine({
  status,
  source,
  title,
  meta,
  liveStep,
  detail,
  nested = false,
  variant = "list",
  onClick,
  testId,
  activityId,
}: {
  /** Undefined while the feed has not listed it yet: a plain muted dot. */
  status?: Activity["status"];
  source?: ActivitySource;
  title: string;
  /** Mono figures on the right: an elapsed time, a duration, a clock time. */
  meta?: ReactNode;
  /** What it is doing right now; shown as "Now · …" while it works. */
  liveStep?: string;
  /** The one muted line under a settled row (an outcome, "Didn't finish"). */
  detail?: string;
  nested?: boolean;
  variant?: "list" | "inline";
  onClick?: () => void;
  testId?: string;
  /** The list row's Activity, so "Show details" from the chat can find it (activityFocus.ts). */
  activityId?: string;
}) {
  const { t } = useLingui();
  const sub = liveStep ? (
    <span aria-live="polite">
      {t`Now`} · {liveStep}
    </span>
  ) : (
    detail
  );

  if (variant === "inline") {
    const body = (
      <>
        <WorkLead status={status} source={source} size={22} />
        <span
          data-testid="activity-line-title"
          className={cn("min-w-0 truncate text-foreground", nested && "text-ink-2")}
          dir="auto"
        >
          {title}
        </span>
        {sub ? (
          <span
            className={cn(
              "min-w-0 truncate text-[12px]",
              status === "failed" ? "text-ink-2" : "text-ink-3",
            )}
            dir="auto"
          >
            · {sub}
          </span>
        ) : null}
        {meta ? (
          <span className="ms-1.5 shrink-0 font-mono text-[12px] whitespace-nowrap text-ink-3 tabular-nums">
            {meta}
          </span>
        ) : null}
      </>
    );
    const className =
      "inline-flex w-fit max-w-full min-w-0 items-center gap-2.5 rounded-xl bg-window py-1.5 ps-1.5 pe-3 text-start text-[13px]";
    if (!onClick) return <div className={className}>{body}</div>;
    return (
      <button
        type="button"
        onClick={onClick}
        data-testid={testId}
        data-status={status}
        className={cn(
          className,
          "transition-colors hover:bg-selection focus-visible:outline-2 focus-visible:outline-ring",
        )}
      >
        {body}
      </button>
    );
  }

  const body = (
    <>
      <span className="grid place-items-center">
        <WorkLead status={status} source={source} size={nested ? 22 : 28} />
      </span>
      <span className="min-w-0">
        <span
          data-testid="activity-line-title"
          className="line-clamp-2 text-[14px] leading-[1.3] tracking-[-0.15px] text-foreground"
          dir="auto"
        >
          {title}
        </span>
        {sub ? (
          <span
            className={cn(
              "mt-px line-clamp-2 block text-[12px] leading-[1.35]",
              status === "failed" ? "text-ink-2" : "text-ink-3",
            )}
            dir="auto"
          >
            {sub}
          </span>
        ) : null}
      </span>
      {meta ? (
        <span className="shrink-0 font-mono text-[12px] whitespace-nowrap text-ink-3 tabular-nums">
          {meta}
        </span>
      ) : (
        <span />
      )}
      {onClick ? <ChevronGlyph className="h-[13px] w-2 text-ink-3 opacity-60" /> : <span />}
    </>
  );
  const className = cn(
    "superchat-row grid min-h-[52px] w-full grid-cols-[28px_minmax(0,1fr)_auto_8px] items-center gap-x-3 py-2 pe-3 text-start",
    nested ? "ps-6" : "ps-3",
  );
  if (!onClick)
    return (
      <div className={className} data-testid={testId} data-activity-id={activityId}>
        {body}
      </div>
    );
  return (
    <button
      type="button"
      onClick={onClick}
      data-testid={testId}
      data-status={status}
      data-activity-id={activityId}
      className={cn(
        className,
        "transition-colors hover:bg-selection focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring",
      )}
    >
      {body}
    </button>
  );
}

/** A Helper's parts under its row. In the inspector they are indented rows of the same
 * group; in the conversation they hang off a thin connector line. */
export function ActivityBranch({
  children,
  variant = "list",
}: {
  children: ReactNode;
  variant?: "list" | "inline";
}) {
  if (variant === "list") return <>{children}</>;
  return (
    <div className="ms-[18px] mt-1 flex flex-col gap-1 border-s border-line ps-2">{children}</div>
  );
}
