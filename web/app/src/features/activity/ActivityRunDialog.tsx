import { plural } from "@lingui/core/macro";
import { useLingui } from "@lingui/react/macro";
import { ChatMarkdown } from "@superchat/chat-ui/web";
import type {
  Activity,
  ActivityStep,
  ThreadMessage,
  ThreadMessagePage,
} from "@superchat/contracts";
import { presentActivityTitle, presentStep } from "@superchat/core";
import { Button, cn, Dialog, DialogContent } from "@superchat/ui-web";
import type { LucideIcon } from "lucide-react";
import { Check, ChevronRight, Circle } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { RunStatusDot } from "../../components/chrome/RunStatusDot";
import { ACTIVITY_SOURCE_ICON, TOOL_ICON_COMPONENT } from "../../components/chrome/toolIcons";
import { ActivityIconTile } from "./ActivityIconTile";
import { formatClockTime, formatDayAndTime, isHelperActivity } from "./activityGrouping";
import { MessageRow } from "./MessageRow";
import type { StoryItem } from "./stepStory";
import { stepStory, tallySteps } from "./stepStory";

/** The Activity run page's wire: full detail (Steps) and, for a Helper, its messages —
 * kept explicit (not the `rpc` client) so the dev fixture page can supply fakes. */
export type ActivityWire = {
  get: (input: { botId: string; activityId: string }) => Promise<Activity>;
  helperMessages: (input: { botId: string; chatId: string }) => Promise<ThreadMessagePage>;
};

function RunStatusPill({ status, label }: { status: Activity["status"]; label: string }) {
  return (
    <span className="inline-flex items-center gap-2 rounded-full border border-border bg-card px-3 py-1 text-[13px] font-medium text-muted-foreground">
      <RunStatusDot status={status} />
      {label}
    </span>
  );
}

/** One Step's icon on the Steps timeline: the tool's own icon (never its raw name —
 * docs/helpers.md "The activity panel"), gently pulsing only for the latest
 * step of a run still in progress (the motion is the signal, not a color), respecting
 * reduced motion via `motion-safe:`. */
function StepIcon({ Icon, live }: { Icon: LucideIcon; live: boolean }) {
  return (
    <span
      aria-hidden="true"
      className="relative z-[1] mt-[2px] flex size-4 shrink-0 items-center justify-center"
    >
      <Icon
        size={14}
        strokeWidth={1.75}
        className={cn(
          "text-muted-foreground",
          live && "motion-safe:animate-[rkPulse_2.4s_ease-in-out_infinite]",
        )}
      />
    </span>
  );
}

/** One Step on the timeline: an icon connected to its neighbors by a thin rule, a title
 * that wraps instead of truncating, and a time on the right. A Step with a snippet gets a
 * chevron in a fixed-width column before its title (native `<details>`, keyboard
 * reachable for free); a Step without one still reserves that column, so every title
 * starts at the same x whether or not its Step is expandable. Expanding it shows only a
 * short snippet of the result — never the full call/result trace, never the tool's name. */
function StepRow({ step, live, isLast }: { step: ActivityStep; live: boolean; isLast: boolean }) {
  const { i18n } = useLingui();
  const presented = presentStep(step);
  const title = (
    <span
      className="min-w-0 flex-1 text-[13px] text-foreground"
      style={{ overflowWrap: "anywhere" }}
      dir="auto"
    >
      {presented.title}
    </span>
  );
  const time = (
    <time className="shrink-0 pt-0.5 text-[11.5px] whitespace-nowrap text-muted-foreground">
      {formatClockTime(step.createdAt, i18n.locale)}
    </time>
  );

  if (presented.snippet === null) {
    return (
      <li className={cn("relative flex gap-3", !isLast && "pb-4")}>
        <StepIcon Icon={TOOL_ICON_COMPONENT[presented.icon]} live={live} />
        <div className="flex min-w-0 flex-1 items-start gap-2">
          <span aria-hidden="true" className="w-4 shrink-0" />
          {title}
          {time}
        </div>
      </li>
    );
  }
  return (
    <li className={cn("relative flex gap-3", !isLast && "pb-4")}>
      <StepIcon Icon={TOOL_ICON_COMPONENT[presented.icon]} live={live} />
      <details className="group min-w-0 flex-1">
        <summary className="flex cursor-pointer list-none items-start gap-2 [&::-webkit-details-marker]:hidden">
          <span aria-hidden="true" className="flex w-4 shrink-0 items-center justify-center pt-0.5">
            <ChevronRight
              size={12}
              strokeWidth={2}
              className="text-muted-foreground transition-transform group-open:rotate-90"
            />
          </span>
          {title}
          {time}
        </summary>
        <p
          className="mt-2 ml-6 line-clamp-3 text-[12.5px] leading-relaxed text-muted-foreground"
          dir="auto"
        >
          {presented.snippet}
        </p>
      </details>
    </li>
  );
}

/** What a progress heading covers, in a few counted words ("3 commands · 1 file"). */
function TallyLine({ steps }: { steps: ActivityStep[] }) {
  const tally = tallySteps(steps);
  const parts = [
    tally.commands ? plural(tally.commands, { one: "# command", other: "# commands" }) : null,
    tally.files ? plural(tally.files, { one: "# file", other: "# files" }) : null,
    tally.looked
      ? plural(tally.looked, { one: "looked at # file", other: "looked at # files" })
      : null,
    tally.other ? plural(tally.other, { one: "# step", other: "# steps" }) : null,
  ].filter((part) => part !== null);
  return (
    <p className="mt-0.5 text-[12px] text-muted-foreground first-letter:uppercase">
      {parts.join(" · ")}
    </p>
  );
}

/** A progress heading: a check once settled, the live mark while it is the run's latest; the
 * Steps it covers fold under it, collapsed until opened (native `<details>`). */
function HeadingRow({
  item,
  live,
  lastStepId,
  running,
  isLast,
}: {
  item: Extract<StoryItem, { kind: "heading" }>;
  live: boolean;
  lastStepId: string | undefined;
  running: boolean;
  isLast: boolean;
}) {
  const { t, i18n } = useLingui();
  const first = item.heading ?? item.steps[0];
  const title = (
    <span
      className="min-w-0 flex-1 text-[13px] font-medium text-foreground"
      style={{ overflowWrap: "anywhere" }}
      dir="auto"
    >
      {item.heading ? presentStep(item.heading).title : t`Getting started`}
    </span>
  );
  const time = first ? (
    <time className="shrink-0 pt-0.5 text-[11.5px] whitespace-nowrap text-muted-foreground">
      {formatClockTime(first.createdAt, i18n.locale)}
    </time>
  ) : null;
  return (
    <li className={cn("relative flex gap-3", !isLast && "pb-4")} data-testid="story-heading">
      <StepIcon Icon={live ? Circle : Check} live={live} />
      {item.steps.length === 0 ? (
        <div className="flex min-w-0 flex-1 items-start gap-2">
          <span aria-hidden="true" className="w-4 shrink-0" />
          {title}
          {time}
        </div>
      ) : (
        <details className="group/heading min-w-0 flex-1">
          <summary className="cursor-pointer list-none [&::-webkit-details-marker]:hidden">
            <span className="flex items-start gap-2">
              <span
                aria-hidden="true"
                className="flex w-4 shrink-0 items-center justify-center pt-0.5"
              >
                <ChevronRight
                  size={12}
                  strokeWidth={2}
                  className="text-muted-foreground transition-transform group-open/heading:rotate-90"
                />
              </span>
              {title}
              {time}
            </span>
            <span className="ml-6 block">
              <TallyLine steps={item.steps} />
            </span>
          </summary>
          <ol className="relative mt-3 ml-6">
            <div aria-hidden="true" className="absolute top-2 bottom-2 left-2 w-px bg-border" />
            {item.steps.map((step, index) => (
              <StepRow
                key={step.itemId}
                step={step}
                live={running && step.itemId === lastStepId}
                isLast={index === item.steps.length - 1}
              />
            ))}
          </ol>
        </details>
      )}
    </li>
  );
}

/** The Steps timeline: one thin vertical rule behind every dot (same proportion as the
 * Goal plan timeline's — PlanTimeline.tsx), newest-last in the order the engine gives
 * them. */
export function StepsTimeline({ steps, running }: { steps: ActivityStep[]; running: boolean }) {
  const story = stepStory(steps);
  if (story) {
    const lastHeading = story.findLastIndex((item) => item.kind === "heading");
    const lastStepId = steps.at(-1)?.itemId;
    return (
      <ol className="relative mt-2" data-testid="activity-steps">
        <div aria-hidden="true" className="absolute top-2 bottom-2 left-2 w-px bg-border" />
        {story.map((item, index) => {
          const isLast = index === story.length - 1;
          return item.kind === "heading" ? (
            <HeadingRow
              key={item.heading?.itemId ?? "getting-started"}
              item={item}
              live={running && index === lastHeading}
              lastStepId={lastStepId}
              running={running}
              isLast={isLast}
            />
          ) : (
            <StepRow
              key={item.step.itemId}
              step={item.step}
              live={running && item.step.itemId === lastStepId}
              isLast={isLast}
            />
          );
        })}
      </ol>
    );
  }
  return (
    <ol className="relative mt-2" data-testid="activity-steps">
      <div aria-hidden="true" className="absolute top-2 bottom-2 left-2 w-px bg-border" />
      {steps.map((step, index) => {
        const isLast = index === steps.length - 1;
        return <StepRow key={step.itemId} step={step} live={running && isLast} isLast={isLast} />;
      })}
    </ol>
  );
}

/** A Helper's messages, read-only (docs/glossary.md "Helper": "Visible, read-only"): no
 * composer, one line saying so, reusing the Side Chat session's own bubble rendering. */
function HelperReadView({
  botId,
  chatId,
  wire,
  onBack,
}: {
  botId: string;
  chatId: string;
  wire: ActivityWire;
  onBack: () => void;
}) {
  const { t } = useLingui();
  const [messages, setMessages] = useState<ThreadMessage[] | null>(null);
  const generation = useRef(0);

  useEffect(() => {
    const current = ++generation.current;
    setMessages(null);
    void wire
      .helperMessages({ botId, chatId })
      .then((page) => {
        if (current === generation.current) setMessages(page.messages);
      })
      .catch(() => {
        if (current === generation.current) setMessages([]);
      });
    return () => {
      generation.current += 1;
    };
  }, [botId, chatId, wire]);

  return (
    <div className="flex min-h-0 flex-col">
      <div className="flex items-center border-b border-border px-6 py-2.5">
        <Button type="button" variant="ghost" size="xs" onClick={onBack}>
          {t`Back to steps`}
        </Button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
        {messages === null ? (
          <p className="text-[13px] text-muted-foreground">{t`Loading…`}</p>
        ) : (
          <div className="flex flex-col gap-4">
            {messages.map((message) => (
              <MessageRow key={message.id} message={message} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * The run page: a full-width header (status, title, time), then Outcome as a full-width
 * block with "View details" inline at its end, then Steps full-width below — the dialog
 * only earns a second column (Steps the wider ~60%) once it's wide enough to hold one
 * (≥900px) sensibly; a Helper Activity's "View details" replaces Outcome+Steps with its
 * read-only messages. Steps load lazily from `activities.get`, since `activities.list`
 * omits them.
 */
export function ActivityRunDialog({
  botId,
  wire,
  activity,
  onOpenChange,
}: {
  botId: string;
  wire: ActivityWire;
  /** The Activity to show, or null when the run page is closed. */
  activity: Activity | null;
  onOpenChange: (open: boolean) => void;
}) {
  const { t, i18n } = useLingui();
  const [detail, setDetail] = useState<Activity | null>(null);
  const [helperOpen, setHelperOpen] = useState(false);
  const generation = useRef(0);

  // Only a genuinely different Activity (or closing) resets the view — a status change
  // on the same one (below) must not kick the person out of the Helper messages they
  // might be reading back to Steps.
  useEffect(() => {
    setDetail(null);
    setHelperOpen(false);
  }, [activity?.id]);

  // Fetches full detail (Steps) when the Activity opens, and again each time the live feed
  // reports a new Step or a settled status (useActivities.ts is the one clock), so the run
  // page never goes stale while it is open and needs no timer of its own.
  const stepCount = activity?.steps?.length ?? 0;
  useEffect(() => {
    if (!activity) return;
    const activityId = activity.id;
    const current = ++generation.current;
    let cancelled = false;
    void wire
      .get({ botId, activityId })
      .then((full) => {
        if (!cancelled && current === generation.current) setDetail(full);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
      generation.current += 1;
    };
  }, [botId, activity?.id, activity?.status, stepCount, wire]);

  if (!activity) return null;
  const steps = detail?.steps ?? [];
  // A failed run with no steps never got going (e.g. its host didn't start): it
  // "couldn't run", it didn't "not finish" — and it always says why.
  const neverRan = activity.status === "failed" && detail !== null && steps.length === 0;
  const outcome =
    activity.outcome ??
    (activity.status === "in_progress"
      ? t`Still working on this.`
      : activity.status === "failed"
        ? t`Something went wrong before it could start.`
        : null);
  const statusLabel = {
    in_progress: t`In progress`,
    done: t`Done`,
    failed: neverRan ? t`Couldn't run` : t`Didn't finish`,
    cancelled: t`Cancelled`,
  }[activity.status];
  const showHelper = helperOpen && isHelperActivity(activity);

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent
        className="grid max-h-[80vh] w-full max-w-[920px] grid-rows-[auto_1fr] gap-0 overflow-hidden p-0 sm:max-w-[920px]"
        aria-label={presentActivityTitle(activity.title)}
      >
        <div className="border-b border-border px-6 py-5 pe-12">
          <RunStatusPill status={activity.status} label={statusLabel} />
          <div className="mt-2.5 flex items-center gap-3">
            <ActivityIconTile
              Icon={ACTIVITY_SOURCE_ICON[activity.source]}
              live={activity.status === "in_progress"}
              failed={activity.status === "failed"}
              size="lg"
            />
            <div className="min-w-0">
              <h3 className="text-[18px] leading-snug font-semibold text-foreground" dir="auto">
                {presentActivityTitle(activity.title)}
              </h3>
              <p className="text-[13px] text-muted-foreground">
                {formatDayAndTime(activity.startedAt, i18n.locale)}
              </p>
            </div>
          </div>
        </div>
        <div
          className={cn(
            "grid min-h-0",
            showHelper ? "grid-cols-1" : "grid-cols-1 min-[900px]:grid-cols-[1fr_1.6fr]",
          )}
        >
          {showHelper ? (
            <HelperReadView
              botId={botId}
              chatId={activity.chatId}
              wire={wire}
              onBack={() => setHelperOpen(false)}
            />
          ) : (
            <>
              <div className="min-h-0 overflow-y-auto border-b border-border px-6 py-5 min-[900px]:border-b-0 min-[900px]:border-e">
                <h4 className="text-[12.5px] font-semibold text-muted-foreground">{t`Outcome`}</h4>
                <div className="mt-1.5 flex items-start justify-between gap-4">
                  {outcome ? (
                    <div
                      // The engine hands Outcome over as one flattened line, so a leading "#"
                      // would turn the whole text into a heading; keep headings at body size.
                      className="min-w-0 flex-1 text-[15px] leading-[1.55] text-foreground [&_:is(h1,h2,h3,h4,h5,h6)]:my-0! [&_:is(h1,h2,h3,h4,h5,h6)]:text-[1em]! [&_:is(h1,h2,h3,h4,h5,h6)]:font-normal! [&_:is(h1,h2,h3,h4,h5,h6)]:leading-[inherit]!"
                      dir="auto"
                    >
                      <ChatMarkdown>{outcome}</ChatMarkdown>
                    </div>
                  ) : (
                    <span className="flex-1" />
                  )}
                  {isHelperActivity(activity) ? (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="shrink-0"
                      onClick={() => setHelperOpen(true)}
                    >
                      {t`View details`}
                    </Button>
                  ) : null}
                </div>
              </div>
              <div className="min-h-0 overflow-y-auto px-6 py-5">
                <h4 className="text-[12.5px] font-semibold text-muted-foreground">{t`Steps`}</h4>
                {detail === null ? (
                  <p className="py-3 text-[13px] text-muted-foreground">{t`Loading…`}</p>
                ) : steps.length === 0 ? (
                  <p className="py-3 text-[13px] text-muted-foreground">{t`No steps recorded`}</p>
                ) : (
                  <StepsTimeline steps={steps} running={activity.status === "in_progress"} />
                )}
              </div>
            </>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
