import { t } from "@lingui/core/macro";
import { useLingui } from "@lingui/react/macro";
import { cn } from "@superchat/ui-web";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  type ActivitiesState,
  ActivityPanel,
  type ActivityWire,
  GROUP_LABEL,
  LIVE_ACTIVITY_WIRE,
  useActivities,
} from "../../../features/activity";
import { AskDecisionCard, useAsks } from "../../../features/approvals";
import { DaysSection, type DaysWire } from "../../../features/daily-notes";
import { MemoryTab, type MemoryWire } from "../../../features/memory";
import { rpc } from "../../../lib/rpc";
import {
  type ConnectionLatch,
  INITIAL_CONNECTION_LATCH,
  nextConnectionLatch,
} from "./connectionStatus";

// The right-hand context panel beside the Conversation: only what matters right now, pulled
// from data the shell already loads elsewhere (Asks, Activity, Memory).
const ASKS_LIMIT = 3;
const STORAGE_KEY = "muse:context-panel-collapsed";

/**
 * Whether the person collapsed the context panel, persisted across sessions. Read once at
 * mount and written on every toggle; a missing or unreadable value defaults to open.
 */
export function useContextPanelCollapsed(): [boolean, (next: boolean) => void] {
  const [collapsed, setCollapsedState] = useState(() => {
    try {
      return window.localStorage.getItem(STORAGE_KEY) === "1";
    } catch {
      return false;
    }
  });
  const setCollapsed = useCallback((next: boolean) => {
    setCollapsedState(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next ? "1" : "0");
    } catch {
      // Best-effort; the toggle still works for the rest of this session.
    }
  }, []);
  return [collapsed, setCollapsed];
}

/** A grouped list's header: the title in ink and a quiet count beside it. */
function PanelSectionHeader({ title, count }: { title: string; count: number }) {
  return (
    <h3 className={GROUP_LABEL}>
      {title}
      <span className="ms-1 font-normal text-ink-3 tabular-nums">{count}</span>
    </h3>
  );
}

type PanelTab = "activity" | "memory";

const PANEL_TABS: { id: PanelTab; label: () => string }[] = [
  { id: "activity", label: () => t`Activity` },
  { id: "memory", label: () => t`Memory` },
];

/** A small segmented control with text labels: one quiet track, the selected segment
 * raised on a solid chip. */
function PanelTabBar({ tab, onChange }: { tab: PanelTab; onChange: (tab: PanelTab) => void }) {
  return (
    <div
      role="tablist"
      aria-label={t`Panel sections`}
      className="grid grid-cols-2 rounded-lg bg-selection p-0.5"
    >
      {PANEL_TABS.map((item) => (
        <button
          key={item.id}
          type="button"
          role="tab"
          aria-selected={tab === item.id}
          data-testid={`context-panel-tab-${item.id}`}
          onClick={() => onChange(item.id)}
          className={cn(
            "h-6 rounded-md text-[12px] transition-colors focus-visible:outline-2 focus-visible:outline-ring",
            tab === item.id
              ? "bg-group text-foreground shadow-[0_1px_2px_rgb(0_0_0/0.14)]"
              : "text-ink-2 hover:text-foreground",
          )}
        >
          {item.label()}
        </button>
      ))}
    </div>
  );
}

/** The panel's one compact identity line: the Muse's name and a small Connection Status pill. */
function IdentityHeader({
  museName,
  connection,
}: {
  museName: string;
  connection: "connected" | "connecting";
}) {
  const { t: tt } = useLingui();
  return (
    <div className="flex min-w-0 items-center gap-2.5 px-1 py-0.5">
      <span
        data-testid="context-panel-muse-name"
        className="min-w-0 flex-1 truncate text-[15px] font-semibold tracking-[-0.2px] text-foreground"
        dir="auto"
      >
        {museName}
      </span>
      <span
        data-testid="context-panel-connection"
        className="inline-flex h-[22px] shrink-0 items-center gap-1.5 rounded-full bg-selection px-2.5 text-[11.5px] text-ink-2"
      >
        <span
          aria-hidden="true"
          className={cn(
            "size-1.5 rounded-full",
            connection === "connected"
              ? "bg-ok"
              : "animate-pulse bg-ink-3 motion-reduce:animate-none",
          )}
        />
        {connection === "connected" ? tt`Connected` : tt`Connecting…`}
      </span>
    </div>
  );
}

/**
 * The Conversation's right-hand context panel (the old design notes): the Muse's identity,
 * then its Activity, Memory, and (what's waiting / in progress / coming up) in a segmented
 * tab bar — nothing that isn't already shown elsewhere, just surfaced beside the chat
 * instead of a click away. Hidden below 1280px (the caller controls that with CSS;
 * `collapsed` is the person's own choice).
 */
export function ContextPanel({
  botId,
  museName,
  collapsed,
  activitiesOverride,
  memoryWire,
  daysWire,
  onOpenWaiting,
}: {
  botId: string;
  /** The Muse's own name, for the panel's identity header. */
  museName: string;
  collapsed: boolean;
  /** Dev-preview seam only (ActivityPreviewPage.tsx): fixture Activity data + wire instead
   * of the real poll. Production never passes this. */
  activitiesOverride?: {
    state: ActivitiesState;
    wire: ActivityWire;
    loadEarlier: () => Promise<void>;
    loadingEarlier: boolean;
  };
  /** Dev-preview seam only: a fixture Memory wire instead of `rpc.memory.profile`. */
  memoryWire?: MemoryWire;
  /** Dev-preview seam only: a fixture daily-notes wire instead of `rpc.dailyNotes`. */
  daysWire?: DaysWire;
  onOpenWaiting: () => void;
}) {
  const { t: tt } = useLingui();
  const [tab, setTab] = useState<PanelTab>("activity");
  const { asks, answer } = useAsks(botId);
  const liveActivities = useActivities(botId);
  const liveMemoryWire = useMemo<MemoryWire>(
    () => ({
      profile: (input) => rpc.memory.profile(input),
      claims: (input) => rpc.memory.claims(input),
      editClaim: (input) => rpc.memory.editClaim(input),
      forgetClaim: (input) => rpc.memory.forgetClaim(input),
    }),
    [],
  );
  const liveDaysWire = useMemo<DaysWire>(
    () => ({
      list: (input) => rpc.dailyNotes.list(input),
      save: (input) => rpc.dailyNotes.save(input),
    }),
    [],
  );
  const activitiesData = activitiesOverride ?? {
    state: liveActivities.state,
    wire: LIVE_ACTIVITY_WIRE,
    loadEarlier: liveActivities.loadEarlier,
    loadingEarlier: liveActivities.loadingEarlier,
  };
  const memoryWireToUse = memoryWire ?? liveMemoryWire;
  const [connectionLatch, setConnectionLatch] = useState<ConnectionLatch>(INITIAL_CONNECTION_LATCH);
  // `activitiesData.state` changes identity on every poll round (success or failure alike,
  // useActivities.ts) — exactly the "once per observed round" cadence the latch wants
  // (connectionStatus.ts's `nextConnectionLatch`).
  useEffect(() => {
    setConnectionLatch((prev) => nextConnectionLatch(prev, activitiesData.state));
  }, [activitiesData.state]);
  const connection = connectionLatch.status;

  const topAsks = asks.slice(0, ASKS_LIMIT);
  return (
    <div
      data-testid="context-panel"
      className={cn(
        "superchat-glass m-2 hidden w-[300px] shrink-0 flex-col gap-2.5",
        !collapsed && "xl:flex",
      )}
    >
      <div className="flex shrink-0 flex-col gap-2.5 px-3 pt-3.5">
        <IdentityHeader museName={museName} connection={connection} />
        <PanelTabBar tab={tab} onChange={setTab} />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto sc-scroll px-3 pb-3.5">
        {tab === "activity" ? (
          <div className="flex flex-col gap-4">
            {topAsks.length > 0 ? (
              <section className="flex flex-col gap-1.5" data-testid="context-panel-decisions">
                <PanelSectionHeader title={tt`Inbox`} count={asks.length} />
                {topAsks.map((ask) => (
                  <AskDecisionCard
                    key={ask.id}
                    ask={ask}
                    onOpenWaiting={onOpenWaiting}
                    onAnswer={(choiceId) =>
                      answer({ askId: ask.id, runId: ask.runId, answer: choiceId })
                    }
                  />
                ))}
                {asks.length > ASKS_LIMIT ? (
                  <button
                    type="button"
                    onClick={onOpenWaiting}
                    className="self-start rounded-md px-1.5 text-[12.5px] text-link transition-colors hover:underline focus-visible:outline-2 focus-visible:outline-ring"
                  >
                    {tt`View all`}
                  </button>
                ) : null}
              </section>
            ) : null}
            <ActivityPanel
              botId={botId}
              wire={activitiesData.wire}
              state={activitiesData.state}
              loadEarlier={activitiesData.loadEarlier}
              loadingEarlier={activitiesData.loadingEarlier}
            />
          </div>
        ) : (
          <MemoryTab
            botId={botId}
            wire={memoryWireToUse}
            days={<DaysSection botId={botId} wire={daysWire ?? liveDaysWire} />}
          />
        )}
      </div>
    </div>
  );
}
