import { useLingui } from "@lingui/react/macro";
import type { ActivityStep, Artifact } from "@superchat/contracts";
import { presentActivityTitle, presentStep } from "@superchat/core";
import { cn } from "@superchat/ui-web";
import { ChevronDown, TriangleAlert } from "lucide-react";
import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { SuperchatOrb } from "../../components/ai/orb";
import { Shimmer } from "../../components/ai/primitives";
import { useFileStudio } from "../../components/cards/context";
import { useNow } from "../../components/chrome/useNow";
import { rpc } from "../../lib/rpc";
import { ArtifactPreviewThumbnail } from "../artifacts";
import { StepsTimeline } from "./ActivityRunDialog";
import { focusActivity } from "./activityFocus";
import { useConversationFiles } from "./conversationFiles";
import { activityFeedFor } from "./useActivities";
import { useFirstMissing } from "./useFirstMissing";
import { useHelperPause } from "./useHelperPause";
import type { StripHelper, StripTurn, StripView, WorkLine } from "./workStripState";
import {
  artifactSaveCount,
  composeStrip,
  deriveWorkStrip,
  flagsOf,
  formatClock,
  helperActivities,
} from "./workStripState";

/** A file a Helper of this reply saved a version of: shown under the strip. */
export interface DraftFile {
  artifactId: string;
  name: string;
  version: number;
  size?: number;
  mimeType?: string;
}

/** The strip's mark: the Superchat orb while the work runs, gone still and grey once it settles. */
export function WorkMark({ live, size = 20 }: { live: boolean; size?: number }) {
  return live ? (
    <SuperchatOrb size={size} state="working" flies={false} />
  ) : (
    <span
      aria-hidden="true"
      data-testid="work-strip-still"
      className="sc-work-still shrink-0 rounded-full"
      style={{ width: size, height: size }}
    />
  );
}

/** A running Helper's line (ADR 0022's strip). */
export function WorkLineRow({ line }: { line: WorkLine }) {
  const { t } = useLingui();
  return (
    <div data-testid="work-strip-line" className="flex min-w-0 flex-col gap-1">
      <div className="flex min-w-0 items-center gap-2">
        <span className="max-w-[55%] shrink-0 truncate font-[560] text-foreground" dir="auto">
          {presentActivityTitle(line.title)}
        </span>
        <span
          data-testid="work-strip-status"
          className="min-w-0 flex-1 truncate text-ink-2"
          dir="auto"
          aria-live="polite"
        >
          · <Shimmer>{line.progress ?? t`Working`}</Shimmer>
        </span>
        {line.elapsedMs !== null ? (
          <span
            data-testid="work-strip-elapsed"
            className="shrink-0 text-[12.5px] text-ink-3 tabular-nums"
          >
            {formatClock(line.elapsedMs)}
          </span>
        ) : null}
      </div>
      {line.flags.map((flag, index) => (
        // Flags only accumulate, oldest first: the position is the flag.
        <Flag key={index} text={flag} />
      ))}
    </div>
  );
}

/** Something a Helper flagged: a warning-tinted line, shown at once. */
function Flag({ text }: { text: string }) {
  return (
    <div
      data-testid="work-strip-flag"
      className="sc-work-flag flex min-w-0 items-start gap-2 rounded-lg bg-warn/12 px-[9px] py-1.5 text-[12.5px] leading-[1.4] text-foreground"
    >
      <TriangleAlert
        aria-hidden="true"
        size={13}
        strokeWidth={2.2}
        className="mt-px shrink-0 text-warning"
      />
      <span className="min-w-0" dir="auto">
        {text}
      </span>
    </div>
  );
}

/** Files the work is writing, as a row of thumbnails filling in; each opens beside the chat. */
export function DraftThumbs({
  drafts,
  updating,
}: {
  drafts: readonly DraftFile[];
  updating: boolean;
}) {
  const studio = useFileStudio();
  return (
    <div data-testid="work-strip-drafts" className="grid grid-cols-6 gap-1.5 pt-0.5">
      {drafts.map((draft) => (
        <button
          key={draft.artifactId}
          type="button"
          data-testid="work-strip-draft"
          data-updating={updating || undefined}
          title={draft.name}
          aria-label={draft.name}
          onClick={() => studio?.open(draft.artifactId, draft.name)}
          className="sc-work-thumb relative aspect-video overflow-hidden rounded-[4px] border border-separator bg-bubble focus-visible:outline-2 focus-visible:outline-ring"
        >
          <ArtifactPreviewThumbnail
            artifact={{
              id: draft.artifactId,
              name: draft.name,
              version: draft.version,
              size: draft.size ?? 0,
              mimeType: draft.mimeType ?? "",
            }}
          />
        </button>
      ))}
    </div>
  );
}

export const LINK =
  "shrink-0 rounded-sm text-[12.5px] text-ink-2 underline-offset-2 transition-colors hover:text-foreground hover:underline focus-visible:outline-2 focus-visible:outline-ring";

/** The engine's one line of what the reply's work did; pressed, the steps unfold beneath it
 * (grouped by the progress the work reported, as the run page shows them). */
export interface StripSummary {
  line: string;
  /** The turn's steps, in full: read when the line is first opened. */
  loadSteps: () => Promise<ActivityStep[]>;
}

function SummaryLine({ summary }: { summary: StripSummary }) {
  const [open, setOpen] = useState(false);
  const [steps, setSteps] = useState<ActivityStep[] | null>(null);
  const { loadSteps } = summary;
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    loadSteps().then(
      (read) => {
        if (!cancelled) setSteps(read);
      },
      () => {
        if (!cancelled) setSteps([]);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [open, loadSteps]);
  return (
    <div className="flex flex-col">
      <button
        type="button"
        data-testid="work-strip-summary"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className={cn(LINK, "inline-flex items-center gap-1 self-start text-start")}
      >
        <span dir="auto">{summary.line}</span>
        <ChevronDown
          aria-hidden="true"
          size={13}
          strokeWidth={2}
          className={cn("shrink-0 transition-transform duration-200", open && "rotate-180")}
        />
      </button>
      {open && steps && steps.length > 0 ? (
        <div data-testid="work-strip-steps">
          <StepsTimeline steps={steps} running={false} />
        </div>
      ) : null}
    </div>
  );
}

/** Pause for a reply's running Helpers; once they are held, "Paused · Resume". */
export interface StripPause {
  paused: boolean;
  onPause: () => void;
  onResume: () => void;
}

export function PauseControl({ pause }: { pause: StripPause }) {
  const { t } = useLingui();
  return pause.paused ? (
    <span data-testid="work-strip-paused" className="shrink-0 text-[12.5px] text-ink-2">
      {t`Paused`} ·{" "}
      <button
        type="button"
        data-testid="work-strip-resume"
        onClick={pause.onResume}
        className={LINK}
      >
        {t`Resume`}
      </button>
    </span>
  ) : (
    <button type="button" data-testid="work-strip-pause" onClick={pause.onPause} className={LINK}>
      {t`Pause`}
    </button>
  );
}

/**
 * The work strip under a reply from before Rounds (ADR 0022, ADR 0027). While the work runs it is
 * lit: a slowly turning edge in the tint family, the orb as its mark, one line per running Helper
 * (else the turn's own latest step), a clock and anything a Helper flagged. Running Helpers can be
 * paused. Settled, it goes still: "Done" and the span, with the engine's summary line, which
 * unfolds to the steps. "Show details" opens the Activity panel on the work.
 */
export function WorkStripView({
  view,
  drafts = [],
  summary = null,
  pause = null,
  liveSteps = NO_STEPS,
  onShowDetails,
}: {
  view: StripView;
  /** A running turn without a Helper: its own steps so far, newest last. */
  liveSteps?: readonly ActivityStep[];
  drafts?: readonly DraftFile[];
  summary?: StripSummary | null;
  /** Shown while the reply has running Helpers. */
  pause?: StripPause | null;
  onShowDetails: () => void;
}) {
  const { t } = useLingui();
  const { live } = view;
  const lastStep = live ? liveSteps.at(-1) : undefined;
  const now =
    view.lines[0]?.title ??
    (lastStep ? presentStep(lastStep).title : null) ??
    view.progress ??
    t`Working`;
  const settled =
    view.outcome === "failed"
      ? t`Didn't finish`
      : view.outcome === "cancelled"
        ? t`Cancelled`
        : t`Done`;
  const clock =
    view.clockMs !== null ? (
      <span
        data-testid="work-strip-clock"
        className="ms-auto shrink-0 text-[12.5px] text-ink-3 tabular-nums"
      >
        {formatClock(view.clockMs)}
      </span>
    ) : (
      <span className="ms-auto" />
    );
  const details = (
    <button type="button" data-testid="work-strip-details" onClick={onShowDetails} className={LINK}>
      {t`Show details`}
    </button>
  );
  const pauseControl = live && pause ? <PauseControl pause={pause} /> : null;
  return (
    <div
      data-testid="work-strip"
      data-state={live ? "working" : "settled"}
      data-live={live || undefined}
      className="sc-work mt-2.5 w-[520px] max-w-full"
    >
      <div className="flex flex-col gap-[9px] rounded-[12.5px] bg-content px-3.5 pt-[11px] pb-2.5">
        {live && view.lines.length > 0 ? (
          <div className="flex min-w-0 items-start gap-2.5 text-[13.5px]">
            <WorkMark live />
            <div className="flex min-w-0 flex-1 flex-col gap-1.5 pt-px">
              {view.lines.map((line) => (
                <WorkLineRow key={line.helperId} line={line} />
              ))}
            </div>
            {pauseControl}
            {details}
          </div>
        ) : (
          <div className="flex min-w-0 items-center gap-2.5 text-[13.5px]">
            <WorkMark live={live} />
            {live ? (
              <span
                key={now}
                data-testid="work-strip-now"
                className="sc-work-in min-w-0 truncate font-[560]"
                dir="auto"
              >
                {now}
              </span>
            ) : (
              <span
                data-testid="work-strip-settled"
                className="min-w-0 truncate font-[560] whitespace-nowrap"
              >
                {settled}
              </span>
            )}
            {clock}
            {pauseControl}
            {details}
          </div>
        )}
        {lastStep ? (
          <div data-testid="work-strip-live-steps">
            <StepsTimeline steps={[...liveSteps]} running />
          </div>
        ) : null}
        {drafts.length > 0 ? <DraftThumbs drafts={drafts} updating={live} /> : null}
        {!live && summary ? <SummaryLine summary={summary} /> : null}
      </div>
    </div>
  );
}

/** The files this reply's work saved: those whose newest version is a save or a live update by
 * one of its Helpers, or the ones its own turn's file cards name. */
export function draftsOf(
  rows: readonly Artifact[],
  helperIds: readonly string[],
  turnFiles: readonly string[] = [],
): DraftFile[] {
  return rows
    .filter(
      (row) =>
        (row.chatId != null && helperIds.includes(row.chatId)) ||
        turnFiles.includes(row.id) ||
        (row.groupId != null && turnFiles.includes(row.groupId)),
    )
    .map((row) => ({
      artifactId: row.id,
      name: row.name,
      version: row.version,
      size: row.size,
      mimeType: row.mimeType,
    }));
}

/** One reply's strip as it reads now: its Helpers live from the shared Activity feed and the
 * reply's own plan and running state (`turn`). Nothing in it runs unless the Conversation's work
 * state says something does (`lit`). */
export function useStripView(
  botId: string,
  helpers: readonly StripHelper[],
  turn: StripTurn | undefined,
  lit: boolean,
) {
  const feed = activityFeedFor(botId);
  const { state: feedState } = useSyncExternalStore(feed.subscribe, feed.getSnapshot);
  const activities = feedState.status === "ready" ? feedState.activities : undefined;
  const turnLive = Boolean(turn?.live);
  const activityId = turn?.activityId ?? null;
  const turnActivity = useMemo(
    () =>
      activityId ? (activities?.find((activity) => activity.id === activityId) ?? null) : null,
    [activities, activityId],
  );
  const firstMissing = useFirstMissing(helpers, activities);
  // The clock ticks while the strip is live; it is told so after the render that finds it live.
  const [ticking, setTicking] = useState(false);
  const now = useNow(ticking);
  const state = useMemo(
    () => deriveWorkStrip(helpers, activities, now, { turnRunning: turnLive, firstMissing, lit }),
    [lit, helpers, activities, now, turnLive, firstMissing],
  );
  const helpersLive = state.kind === "working";
  const live = helpersLive || turnLive;
  useEffect(() => setTicking(live), [live]);
  const listed = useMemo(() => helperActivities(helpers, activities), [helpers, activities]);
  const view = useMemo(
    () => composeStrip(state, listed.flatMap(flagsOf), turn, now),
    [state, listed, turn, now],
  );
  // A Helper that started a moment ago may not be listed yet: ask the feed once more.
  const missing = helpersLive && feedState.status === "ready" && listed.length < helpers.length;
  useEffect(() => {
    if (missing) feed.refresh();
  }, [feed, missing]);
  return { view, state, listed, turnActivity, activities };
}

/** The work strip for one reply (`useStripView`), with its files, its summary line, Pause for its
 * running Helpers and Comment on its steps. */
export function WorkStrip({
  botId,
  helpers,
  turn,
  lit,
}: {
  botId: string;
  helpers: readonly StripHelper[];
  turn?: StripTurn;
  /** Anything in the Conversation runs (the work state's `running`). */
  lit: boolean;
}) {
  const { view, state, listed, turnActivity } = useStripView(botId, helpers, turn, lit);
  const turnFiles = turn?.files ?? NO_FILES;
  // Read the files once the work has saved one.
  const saved = turnFiles.length > 0 || artifactSaveCount(listed) > 0;
  const rows = useConversationFiles(botId, saved);
  const helperKey = listed.map((activity) => activity.chatId).join(" ");
  const drafts = useMemo(
    () => draftsOf(rows, helperKey ? helperKey.split(" ") : [], turnFiles),
    [rows, helperKey, turnFiles],
  );
  const turnActivityId = turnActivity?.id;
  const summaryLine = turnActivity?.summaryLine;
  const summary = useMemo<StripSummary | null>(
    () =>
      summaryLine && turnActivityId
        ? {
            line: summaryLine,
            loadSteps: () =>
              rpc.activities
                .get({ botId, activityId: turnActivityId })
                .then((full) => full.steps ?? []),
          }
        : null,
    [botId, summaryLine, turnActivityId],
  );
  // The running Helpers the feed lists: Pause holds them, and their entries say when it has.
  const running = useMemo(() => {
    const ids = state.kind === "working" ? state.lines.map((line) => line.helperId) : [];
    return listed.filter((activity) => ids.includes(activity.chatId));
  }, [state, listed]);
  const { paused, pause: onPause, resume: onResume } = useHelperPause(botId, running);
  const pause = useMemo<StripPause | null>(
    () =>
      running.length > 0
        ? { paused, onPause: () => void onPause(), onResume: () => void onResume() }
        : null,
    [running.length, paused, onPause, onResume],
  );
  // A settled reply with no Helper and no steps of its own has nothing to show.
  if (!view.live && helpers.length === 0 && !turnActivity?.summaryLine) return null;
  return (
    <WorkStripView
      view={view}
      drafts={drafts}
      summary={summary}
      pause={pause}
      liveSteps={
        view.live && view.lines.length === 0 ? (turnActivity?.steps ?? NO_STEPS) : NO_STEPS
      }
      onShowDetails={() => focusActivity(view.focusId ?? turnActivity?.id ?? null)}
    />
  );
}

const NO_FILES: readonly string[] = [];
const NO_STEPS: readonly ActivityStep[] = [];
