import { useLingui } from "@lingui/react/macro";
import type { Activity } from "@superchat/contracts";
import { presentActivityTitle } from "@superchat/core";
import { Button } from "@superchat/ui-web";
import type { RefObject } from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import { PanelRowSkeletonList } from "../../components/chrome/PanelSkeleton";
import { useNow } from "../../components/chrome/useNow";
import { ActivityBranch, ActivityLine } from "./ActivityLine";
import { ActivityRunDialog, type ActivityWire } from "./ActivityRunDialog";
import { settleActivityFocus, useActivityFocus } from "./activityFocus";
import {
  activityDurationMs,
  activityLineText,
  formatClockTime,
  formatDuration,
  groupActivitiesByDate,
  groupStudioTurns,
  isRunning,
} from "./activityGrouping";
import {
  type ActivityNode,
  buildActivityForest,
  type FileEntry,
  gatherRequestFiles,
  isNodeRunning,
  latestStepTitle,
  requestEnd,
  runningActivity,
} from "./activityTree";
import type { ActivitiesState } from "./useActivities";

/** One Activity as the "Working" line (ActivityLine): a status dot, its title, and a mono
 * figure on the right. Working, the line under the title is "Now · <live step>" and the figure
 * is how long it has gone; settled, the line is the outcome's gist and the figure is the clock.
 * A Helper's parts, and a request's files, hang beneath it on a connector line. A request whose
 * work outlives its own turn reads as running until that work ends, then as its end. */
function ActivityRow({
  node,
  files = [],
  depth = 0,
  now,
  onOpen,
}: {
  node: ActivityNode;
  files?: readonly FileEntry[];
  depth?: number;
  now: number;
  onOpen: (activityId: string) => void;
}) {
  const { activity, children } = node;
  const { t, i18n } = useLingui();
  const running = runningActivity(node);
  const live = running !== undefined;
  const end = live ? activity : requestEnd(node);
  const duration = live
    ? activityDurationMs({ ...activity, status: "in_progress" as const }, now)
    : null;
  return (
    <>
      <ActivityLine
        status={live ? "in_progress" : end.status}
        source={activity.source}
        title={presentActivityTitle(activity.title)}
        meta={
          live ? (
            <span data-testid="activity-elapsed">
              {duration === null ? "" : formatDuration(duration)}
            </span>
          ) : (
            <time dateTime={activity.startedAt}>
              {formatClockTime(activity.startedAt, i18n.locale)}
            </time>
          )
        }
        liveStep={live ? (latestStepTitle(running) ?? t`Starting`) : undefined}
        detail={activityLineText(end, {
          in_progress: t`Working`,
          done: t`Done`,
          failed: t`Didn't finish`,
          cancelled: t`Cancelled`,
        })}
        nested={depth > 0}
        onClick={() => onOpen(activity.id)}
        testId="activity-row"
        activityId={activity.id}
      />
      {children.length > 0 || files.length > 0 ? (
        <ActivityBranch>
          {children.map((child) => (
            <ActivityRow
              key={child.activity.id}
              node={child}
              depth={depth + 1}
              now={now}
              onOpen={onOpen}
            />
          ))}
          {files.map((file) => (
            <FileRows
              key={`file:${file.groupId}`}
              file={file}
              depth={depth + 1}
              now={now}
              onOpen={onOpen}
            />
          ))}
        </ActivityBranch>
      ) : null}
    </>
  );
}

/** A file's studio turns under its name (ADR 0024): the name as a header line, its turns as
 * nested rows titled from each request. */
function FileRows({
  file,
  depth = 0,
  now,
  onOpen,
}: {
  file: FileEntry;
  depth?: number;
  now: number;
  onOpen: (activityId: string) => void;
}) {
  return (
    <>
      <ActivityLine
        status={file.activities.some(isRunning) ? "in_progress" : "done"}
        source="studio"
        title={file.name}
        nested={depth > 0}
        testId="activity-file"
      />
      <ActivityBranch>
        {file.activities.map((activity) => (
          <ActivityRow
            key={activity.id}
            node={{ activity, children: [] }}
            depth={depth + 1}
            now={now}
            onOpen={onOpen}
          />
        ))}
      </ActivityBranch>
    </>
  );
}

/** The Activities of one stretch of the panel: a request with its work and files beneath it,
 * and a file not tied to a listed request under its own name. */
function ActivityEntries({
  activities,
  nodeByActivityId,
  filesByRound,
  now,
  onOpen,
}: {
  activities: readonly Activity[];
  nodeByActivityId: ReadonlyMap<string, ActivityNode>;
  filesByRound: ReadonlyMap<string, FileEntry[]>;
  now: number;
  onOpen: (activityId: string) => void;
}) {
  const nodeOf = (activity: Activity) =>
    nodeByActivityId.get(activity.id) ?? { activity, children: [] };
  return groupStudioTurns(activities).map((entry) =>
    entry.kind === "activity" ? (
      <ActivityRow
        key={entry.activity.id}
        node={nodeOf(entry.activity)}
        files={entry.activity.roundId ? filesByRound.get(entry.activity.roundId) : undefined}
        now={now}
        onOpen={onOpen}
      />
    ) : (
      <FileRows key={`file:${entry.groupId}`} file={entry} now={now} onOpen={onOpen} />
    ),
  );
}

const EMPTY_ACTIVITIES: Activity[] = [];

/** A grouped list's header ("Working now 2", "Today"): 13px semibold ink, a quiet count. */
export const GROUP_LABEL =
  "px-1.5 pt-1 text-[13px] font-semibold tracking-[-0.1px] text-foreground";
const GROUP_COUNT = "ms-1 font-normal text-ink-3 tabular-nums";

/** How long the skeleton may stand in before the panel settles on its empty line instead. */
export const ACTIVITY_LOADING_GRACE_MS = 4_000;

/**
 * True once a load has run past its grace period, or straight away while the page is hidden
 * (the feed pauses then, so the read the skeleton waits on is not coming): either way the
 * panel shows a quiet loading line rather than a skeleton that never ends.
 */
function useLoadingOverdue(loading: boolean): boolean {
  const [overdue, setOverdue] = useState(false);
  useEffect(() => {
    if (!loading) {
      setOverdue(false);
      return;
    }
    if (document.visibilityState === "hidden") {
      setOverdue(true);
      return;
    }
    const timer = window.setTimeout(() => setOverdue(true), ACTIVITY_LOADING_GRACE_MS);
    return () => window.clearTimeout(timer);
  }, [loading]);
  return overdue;
}

/** "Show details" from the chat (activityFocus.ts): once the list is here, bring the asked-for
 * row into view with the transcript's one-time wash (styles.css `sc-message-flash`). */
function useFocusedRow(
  listRef: RefObject<HTMLDivElement | null>,
  status: ActivitiesState["status"],
) {
  const focus = useActivityFocus();
  useEffect(() => {
    if (!focus || status === "loading") return;
    const row = focus.activityId
      ? listRef.current?.querySelector<HTMLElement>(
          `[data-activity-id="${CSS.escape(focus.activityId)}"]`,
        )
      : null;
    settleActivityFocus(focus.nonce);
    if (!row) return;
    const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    row.scrollIntoView?.({ block: "nearest", behavior: reducedMotion ? "auto" : "smooth" });
    row.classList.remove("sc-message-flash");
    void row.offsetWidth;
    row.classList.add("sc-message-flash");
    row.addEventListener("animationend", () => row.classList.remove("sc-message-flash"), {
      once: true,
    });
  }, [focus, status, listRef]);
}

/** A slow or paused first read: say it is loading, never "Nothing yet", which would claim the
 * person has no Activity when the read simply has not come back. */
function ActivitiesStillLoading() {
  const { t } = useLingui();
  return (
    <p className="px-3 py-8 text-center text-[12.5px] text-ink-3" data-testid="activity-loading">
      {t`Loading…`}
    </p>
  );
}

/** The same calm line, whichever of "truly nothing yet" or "this Muse has no
 * Conversation yet" (NOT_FOUND) it's for — both read the same to the person. */
function EmptyActivities() {
  const { t } = useLingui();
  return (
    <p className="px-3 py-8 text-center text-[12.5px] text-ink-3" data-testid="activity-empty">
      {t`Nothing yet — once I do something, it shows up here.`}
    </p>
  );
}

/**
 * The Activity panel's ledger (docs/helpers.md "The activity panel"): a quiet
 * log beside the Conversation, one calm line per piece of work, grouped by day, newest
 * first. Opening a line opens the run page (ActivityRunDialog), which is kept resynced
 * to this same list by id, so it never shows a stale snapshot while the poll updates
 * the row behind it.
 */
export function ActivityPanel({
  botId,
  wire,
  state,
  loadEarlier,
  loadingEarlier,
}: {
  botId: string;
  wire: ActivityWire;
  state: ActivitiesState;
  loadEarlier: () => Promise<void>;
  loadingEarlier: boolean;
}) {
  const { t, i18n } = useLingui();
  const [openActivityId, setOpenActivityId] = useState<string | null>(null);
  const activities = state.status === "ready" ? state.activities : EMPTY_ACTIVITIES;
  // Helpers nest under the Helper or request that started them, and a file's edits under the
  // request that made it; whatever is running sits above the days.
  const { working, groups, nodeByActivityId, filesByRound } = useMemo(() => {
    const forest = buildActivityForest(activities);
    const byId = new Map<string, ActivityNode>();
    const index = (node: ActivityNode) => {
      byId.set(node.activity.id, node);
      node.children.forEach(index);
    };
    forest.forEach(index);
    const { roots, filesByRound } = gatherRequestFiles(forest);
    const busy = (node: ActivityNode) =>
      isNodeRunning(node) ||
      (filesByRound.get(node.activity.roundId ?? "") ?? []).some((file) =>
        file.activities.some(isRunning),
      );
    return {
      working: roots.filter(busy),
      groups: groupActivitiesByDate(
        roots.filter((node) => !busy(node)).map((node) => node.activity),
        new Date(),
        i18n.locale,
      ),
      nodeByActivityId: byId,
      filesByRound,
    };
  }, [activities, i18n.locale]);
  const now = useNow(working.length > 0);
  const loadingOverdue = useLoadingOverdue(state.status === "loading");
  const listRef = useRef<HTMLDivElement>(null);
  useFocusedRow(listRef, state.status);

  // Both read as "nothing to show yet" — a brand new Muse with no Conversation
  // (NOT_FOUND) looks the same here as a feature not wired up in this environment
  // (NOT_IMPLEMENTED); neither is an error.
  if (state.status === "unavailable") return <EmptyActivities />;
  if (state.status === "loading") {
    return loadingOverdue ? <ActivitiesStillLoading /> : <PanelRowSkeletonList count={4} />;
  }
  if (state.status === "error") {
    return <p className="text-[13px] text-muted-foreground">{t`Could not load Activity`}</p>;
  }

  // Resynced by id every render, not the clicked snapshot — so the run page reflects
  // whatever the latest read merged into `state.activities` (activityFeed.ts).
  const openActivity = (openActivityId && nodeByActivityId.get(openActivityId)?.activity) || null;

  if (working.length === 0 && groups.length === 0) {
    return <EmptyActivities />;
  }

  return (
    <div ref={listRef} className="flex flex-col gap-4" data-testid="activity-panel">
      {working.length > 0 ? (
        <section className="flex flex-col gap-1.5" data-testid="activity-working">
          <h3 className={GROUP_LABEL}>
            {t`Working now`}
            <span className={GROUP_COUNT}>{working.length}</span>
          </h3>
          <div className="superchat-group">
            <ActivityEntries
              activities={working.map((node) => node.activity)}
              nodeByActivityId={nodeByActivityId}
              filesByRound={filesByRound}
              now={now}
              onOpen={setOpenActivityId}
            />
          </div>
        </section>
      ) : null}
      {groups.map((group) => (
        <section key={group.date} className="flex flex-col gap-1.5">
          <h3 className={GROUP_LABEL}>
            {group.label.kind === "today"
              ? t`Today`
              : group.label.kind === "yesterday"
                ? t`Yesterday`
                : group.label.text}
          </h3>
          <div className="superchat-group">
            <ActivityEntries
              activities={group.activities}
              nodeByActivityId={nodeByActivityId}
              filesByRound={filesByRound}
              now={now}
              onOpen={setOpenActivityId}
            />
          </div>
        </section>
      ))}
      {state.hasMore ? (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="self-start text-muted-foreground"
          disabled={loadingEarlier}
          onClick={() => void loadEarlier()}
        >
          {loadingEarlier ? t`Loading…` : t`Load earlier`}
        </Button>
      ) : null}
      <ActivityRunDialog
        botId={botId}
        wire={wire}
        activity={openActivity}
        onOpenChange={(open) => {
          if (!open) setOpenActivityId(null);
        }}
      />
    </div>
  );
}
