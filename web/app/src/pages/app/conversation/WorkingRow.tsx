import { Shimmer } from "../../../components/ai/primitives";
import { useNow } from "../../../components/chrome/useNow";
import { formatClock, serverNow } from "../../../features/activity";
import { WorkingDot } from "../../../features/activity/ActivityLine";

/**
 * Superchat working on a reply (the old design notes "Working line"): a soft rounded row with a
 * small pulsing dot, the current step ("Thinking…", "Browsing…") and how long it has gone —
 * counted from the work's start on the engine's clock (the work state's `since`), so it reads the
 * same as the header pill and keeps its time across a remount.
 */
export function WorkingRow({ label, since }: { label: string; since: number | null }) {
  const now = useNow(true);
  return (
    <div data-testid="muse-live-row" aria-hidden="true" className="flex min-h-10 items-center">
      <span className="inline-flex items-center gap-2.5 rounded-xl bg-window py-1.5 ps-1.5 pe-3 text-[13px]">
        <span className="grid size-[22px] place-items-center">
          <WorkingDot />
        </span>
        <span className="text-ink-2">
          <Shimmer>{label}</Shimmer>
        </span>
        {since !== null ? (
          <span className="ms-1.5 font-mono text-[12px] text-ink-3 tabular-nums">
            {formatClock(serverNow(now) - since)}
          </span>
        ) : null}
      </span>
    </div>
  );
}
