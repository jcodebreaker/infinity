import { plural } from "@lingui/core/macro";
import { useLingui } from "@lingui/react/macro";
import type { Activity, RoundView } from "@superchat/contracts";
import { presentActivityTitle, presentStep } from "@superchat/core";
import { cn } from "@superchat/ui-web";
import { Check, ChevronDown, X } from "lucide-react";
import { useMemo, useState, useSyncExternalStore } from "react";
import { Shimmer } from "../../components/ai/primitives";
import { useNow } from "../../components/chrome/useNow";
import { useConversationFiles } from "./conversationFiles";
import { useConversationWaiting, useRoundView } from "./rounds";
import { serverNow } from "./superchatWork";
import { activityFeedFor } from "./useActivities";
import { useHelperPause } from "./useHelperPause";
import type { DraftFile } from "./WorkStrip";
import { DraftThumbs, PauseControl, WorkMark } from "./WorkStrip";
import { formatClock } from "./workStripState";

// The strip under a person's message that opened a Round (ADR 0029): the engine's record of the
// Round, folded, and nothing inferred. While it runs: the tool running now, the time since the
// Round began, one row per Helper with its folder and its own latest tool, the model's checklist if
// it wrote one, and the files as they land; never a fraction or a count of steps as progress.
// Settled, one line (the files it saved, or how it ended, and how long it took) that unfolds to
// the trail; a Round that has delivered its answer reads settled while its turn winds down. The
// Stop line is in the transcript where the engine wrote it, not here.

type Tool = Pick<RoundView["trail"][number], "name" | "digest">;

/** A tool call in words: the tool registry's title, with the harness's digest of its input. */
function ToolWords({ tool }: { tool: Tool }) {
  const title = tool.name ? presentStep({ tool: tool.name, title: tool.name }).title : null;
  return (
    <>
      {title ? <span dir="auto">{title}</span> : null}
      {tool.digest ? (
        <span className="text-ink-3" dir="auto">
          {title ? " · " : ""}
          {tool.digest}
        </span>
      ) : null}
    </>
  );
}

type Helper = RoundView["helpers"][number];

/** One Helper: its title and folder, beneath them its own latest tool while it runs (else how it
 * ended). */
function HelperRow({ helper, tool }: { helper: Helper; tool: Tool | undefined }) {
  const { t } = useLingui();
  const running = helper.status === "running";
  const ended =
    helper.status === "failed" ? t`Didn't finish` : helper.status === "stopped" ? t`Stopped` : null;
  return (
    <div data-testid="round-helper" data-status={helper.status} className="flex min-w-0 flex-col">
      <span data-testid="round-helper-title" className="min-w-0 truncate" dir="auto">
        <span className="font-[560] text-foreground">
          {presentActivityTitle(helper.title ?? "")}
        </span>
        {helper.folder ? (
          <span data-testid="round-helper-folder" className="text-ink-3">
            {" · "}
            {helper.folder}
          </span>
        ) : null}
      </span>
      {running ? (
        <span
          data-testid="round-helper-live"
          className="min-w-0 truncate text-[12.5px] text-ink-2"
          aria-live="polite"
        >
          <Shimmer>{tool ? <ToolWords tool={tool} /> : t`Working`}</Shimmer>
        </span>
      ) : ended ? (
        <span className="min-w-0 truncate text-[12.5px] text-ink-2">{ended}</span>
      ) : null}
    </div>
  );
}

/** The model's checklist in its own words, each item ticked by its own status. */
function Checklist({ items }: { items: RoundView["checklist"] }) {
  return (
    <ul
      data-testid="round-checklist"
      className="m-0 flex list-none flex-col gap-1 p-0 text-[12.5px] text-foreground"
    >
      {items.map((item, index) => (
        <li
          // The model's ids are optional: its order is the item's place.
          key={item.id ?? index}
          data-status={item.status}
          className="flex min-w-0 items-center gap-2"
        >
          {item.status === "completed" ? (
            <span className="grid size-[13px] shrink-0 place-items-center rounded-full bg-foreground text-content">
              <Check size={9} strokeWidth={3} />
            </span>
          ) : (
            <span
              className={cn(
                "size-[11px] shrink-0 rounded-full border-[1.5px]",
                item.status === "in_progress" ? "border-foreground" : "border-ink-3 opacity-70",
              )}
            />
          )}
          <span
            className={cn("min-w-0 truncate", item.status === "completed" && "text-ink-2")}
            dir="auto"
          >
            {item.status === "in_progress" ? (item.activeForm ?? item.subject) : item.subject}
          </span>
        </li>
      ))}
    </ul>
  );
}

/** Every finished tool call of the Round, in order. */
function Trail({ view }: { view: RoundView }) {
  return (
    <ol
      data-testid="round-trail"
      className="m-0 flex list-none flex-col gap-1 p-0 text-[12.5px] text-ink-2"
    >
      {view.trail.map((step, index) => (
        // The trail only grows: the place is the step.
        <li key={index} data-ok={step.ok} className="flex min-w-0 items-center gap-1.5 truncate">
          {step.ok ? null : (
            <X
              aria-hidden="true"
              size={12}
              strokeWidth={2.4}
              className="shrink-0 text-destructive"
            />
          )}
          <span className="min-w-0 truncate">
            <ToolWords tool={step} />
          </span>
        </li>
      ))}
    </ol>
  );
}

/** The Round's files as the strip's tiles, with what the Library knows of each. */
function useRoundDrafts(botId: string, view: RoundView | null): DraftFile[] {
  const files = view?.files ?? NO_FILES;
  const rows = useConversationFiles(botId, files.length > 0);
  return useMemo(
    () =>
      files.flatMap((file) => {
        if (!file.artifactId) return [];
        const row = rows.find((r) => r.id === file.artifactId || r.groupId === file.artifactId);
        return [
          {
            artifactId: file.artifactId,
            name: file.name ?? row?.name ?? "",
            version: file.version ?? row?.version ?? 1,
            ...(row ? { size: row.size, mimeType: row.mimeType } : {}),
          },
        ];
      }),
    [files, rows],
  );
}

/** Pause for the Round's running Helpers; the engine's hold reads from their Activities. */
function useRoundPause(botId: string, view: RoundView | null) {
  const feed = activityFeedFor(botId);
  const { state } = useSyncExternalStore(feed.subscribe, feed.getSnapshot);
  const activities = state.status === "ready" ? state.activities : NO_ACTIVITIES;
  const ids = (view?.helpers ?? [])
    .filter((helper) => helper.status === "running")
    .map((helper) => helper.id)
    .join(" ");
  const running = useMemo(
    () =>
      ids
        ? ids.split(" ").map(
            (id) =>
              activities.find((activity) => activity.chatId === id) ?? {
                chatId: id,
                paused: false,
              },
          )
        : [],
    [ids, activities],
  );
  const { paused, pause, resume } = useHelperPause(botId, running);
  return running.length > 0
    ? { paused, onPause: () => void pause(), onResume: () => void resume() }
    : null;
}

/** The strip for the Round the person's message opened; nothing until its record is read, and
 * nothing for a settled Round that used no tool. */
export function RoundStrip({ botId, roundId }: { botId: string; roundId: string }) {
  const { t } = useLingui();
  const view = useRoundView(botId, roundId);
  const waiting = useConversationWaiting(botId);
  const live = view?.status === "running" && view.deliveredAt === null;
  const now = useNow(live);
  const drafts = useRoundDrafts(botId, view);
  const pause = useRoundPause(botId, view);
  const [open, setOpen] = useState(false);
  if (
    !view ||
    (view.status !== "running" &&
      view.status !== "done" &&
      view.status !== "stopped" &&
      view.status !== "failed")
  ) {
    return null;
  }
  const worked =
    view.trail.length > 0 ||
    view.helpers.length > 0 ||
    view.files.length > 0 ||
    view.checklist.length > 0;
  if (!live && !worked) return null;
  // A delivered Round's time is its time to the answer, before and after its turn ends.
  const clockMs = live
    ? view.startedAt === null
      ? null
      : Math.max(0, serverNow(now) - view.startedAt)
    : view.deliveredAt !== null && view.startedAt !== null
      ? Math.max(0, view.deliveredAt - view.startedAt)
      : view.elapsedMs;
  const clock =
    clockMs === null ? (
      <span className="ms-auto" />
    ) : (
      <span
        data-testid="round-strip-clock"
        className="ms-auto shrink-0 text-[12.5px] text-ink-3 tabular-nums"
      >
        {formatClock(clockMs)}
      </span>
    );
  // The Assistant's own tool; a Helper's shows on its row.
  const own = view.now && !view.now.helperId ? view.now : null;
  const helperTool = (id: string): Tool | undefined =>
    view.now?.helperId === id ? view.now : view.trail.findLast((step) => step.helperId === id);
  const settled =
    view.status === "stopped"
      ? t`Stopped`
      : view.status === "failed"
        ? t`Didn't finish`
        : view.files.length > 0
          ? plural(view.files.length, { one: "# file", other: "# files" })
          : t`Done`;
  const shown = live || open;
  return (
    <div
      data-testid="round-strip"
      data-state={live ? "working" : "settled"}
      data-live={live || undefined}
      className="sc-work mt-2.5 w-[520px] max-w-full"
    >
      <div className="flex flex-col gap-[9px] rounded-[12.5px] bg-content px-3.5 pt-[11px] pb-2.5">
        {live ? (
          <div className="flex min-w-0 items-center gap-2.5 text-[13.5px]">
            <WorkMark live />
            <span
              key={`${own?.name}:${own?.digest}`}
              data-testid="round-strip-now"
              className="sc-work-in min-w-0 truncate font-[560]"
            >
              {own ? <ToolWords tool={own} /> : (waiting ?? t`Working`)}
            </span>
            {clock}
            {pause ? <PauseControl pause={pause} /> : null}
          </div>
        ) : (
          <button
            type="button"
            data-testid="round-strip-settled"
            aria-expanded={open}
            onClick={() => setOpen((value) => !value)}
            className="flex min-w-0 items-center gap-2.5 rounded-sm text-start text-[13.5px] focus-visible:outline-2 focus-visible:outline-ring"
          >
            <WorkMark live={false} />
            <span className="min-w-0 truncate font-[560] whitespace-nowrap">{settled}</span>
            {clock}
            <ChevronDown
              aria-hidden="true"
              size={13}
              strokeWidth={2}
              className={cn(
                "shrink-0 text-ink-2 transition-transform duration-200",
                open && "rotate-180",
              )}
            />
          </button>
        )}
        {shown && view.helpers.length > 0 ? (
          <div className="flex min-w-0 flex-col gap-1.5 text-[13.5px]">
            {view.helpers.map((helper) => (
              <HelperRow key={helper.id} helper={helper} tool={helperTool(helper.id)} />
            ))}
          </div>
        ) : null}
        {shown && view.checklist.length > 0 ? <Checklist items={view.checklist} /> : null}
        {shown && drafts.length > 0 ? <DraftThumbs drafts={drafts} updating={live} /> : null}
        {open && !live && view.trail.length > 0 ? <Trail view={view} /> : null}
      </div>
    </div>
  );
}

const NO_FILES: RoundView["files"] = [];
const NO_ACTIVITIES: readonly Activity[] = [];
