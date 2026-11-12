import { useLingui } from "@lingui/react/macro";
import { cn } from "@superchat/ui-web";
import { ChevronDown, Clock, Search } from "lucide-react";
import { useMemo, useState } from "react";
import {
  type ActivitiesState,
  ActivityPanel,
  LIVE_ACTIVITY_WIRE,
  useActivities,
} from "../../../features/activity";

/** The search narrows the listed runs by what they say; the feed itself is untouched. */
function narrow(state: ActivitiesState, needle: string): ActivitiesState {
  if (state.status !== "ready" || !needle) return state;
  return {
    ...state,
    activities: state.activities.filter((activity) =>
      [activity.title, activity.outcome, activity.summary].some((text) =>
        text?.toLocaleLowerCase().includes(needle),
      ),
    ),
  };
}

/**
 * The home's bottom-right glass card: the agents' activity (the same Activity panel the
 * Conversation's inspector shows), grouped by day, under a clock-and-search row.
 */
export function HomeActivity({
  botId,
  className,
  onHide,
  hideWhenEmpty = false,
}: {
  botId: string;
  className?: string;
  /** In the Conversation: folds the card back into its pill. */
  onHide?: () => void;
  /** The home hides the card until something has run; the Conversation always shows it. */
  hideWhenEmpty?: boolean;
}) {
  const { t } = useLingui();
  const { state, loadEarlier, loadingEarlier } = useActivities(botId);
  const [query, setQuery] = useState("");
  const needle = query.trim().toLocaleLowerCase();
  const shown = useMemo(() => narrow(state, needle), [state, needle]);
  // Nothing has run yet (a fresh start): no empty card, the home stays clean.
  if (hideWhenEmpty && (state.status !== "ready" || state.activities.length === 0)) return null;
  return (
    <section
      data-testid="home-activity"
      aria-label={t`Activity`}
      className={cn(
        "sc-glass sc-activity flex [&_.font-mono]:font-sans max-h-[min(46vh,420px)] w-full flex-col gap-3 rounded-[20px] p-3.5 md:w-[380px]",
        className,
      )}
    >
      <div className="relative z-10 flex shrink-0 items-center gap-2">
        <span
          aria-hidden="true"
          className="grid size-11 shrink-0 place-items-center rounded-xl text-foreground"
        >
          <Clock size={20} strokeWidth={1.7} />
        </span>
        <div className="relative flex-1">
          <label className="sr-only" htmlFor="home-activity-search">
            {t`Search activity`}
          </label>
          <Search
            size={16}
            strokeWidth={2}
            aria-hidden="true"
            className="pointer-events-none absolute start-3.5 top-3.5 text-ink-2"
          />
          <input
            id="home-activity-search"
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t`Search activity`}
            className="min-h-11 w-full rounded-xl border border-glass-line bg-black/20 ps-10 pe-3.5 text-[14px] text-foreground placeholder:text-ink-2 focus-visible:outline-2 focus-visible:outline-ring"
          />
        </div>
        {onHide ? (
          <button
            type="button"
            aria-label={t`Hide activity`}
            title={t`Hide activity`}
            onClick={onHide}
            className="grid size-11 shrink-0 place-items-center rounded-xl text-foreground transition-colors hover:bg-foreground/10 focus-visible:outline-2 focus-visible:outline-ring"
          >
            <ChevronDown size={18} strokeWidth={2} />
          </button>
        ) : null}
      </div>
      <div className="sc-scroll relative -mx-1 min-h-0 flex-1 overflow-y-auto overscroll-contain px-1">
        <ActivityPanel
          botId={botId}
          wire={LIVE_ACTIVITY_WIRE}
          state={shown}
          loadEarlier={loadEarlier}
          loadingEarlier={loadingEarlier}
        />
      </div>
    </section>
  );
}
