import { useLingui } from "@lingui/react/macro";
import { SuperchatOrb } from "../../components/ai/orb";
import { useNow } from "../../components/chrome/useNow";
import { useConversationWaiting } from "./rounds";
import { serverNow } from "./superchatWork";
import type { WorkState } from "./workState";
import { formatClock } from "./workStripState";

/**
 * The lit state in the conversation header (ADR 0023): while the Conversation's work runs, the orb
 * and "Working · 0:12" in a pill whose edge carries a turning glow, so running work shows even
 * when the strip under its reply is scrolled away; "Paused" while Pause holds every running
 * Helper, and the engine's words while the turn waits before its work starts. Nothing while everything has settled.
 */
export function WorkingPill({
  work,
  botId,
}: {
  work: Pick<WorkState, "running" | "since"> & { paused?: boolean };
  /** The Muse whose Conversation it shows (its waits), once one is open. */
  botId?: string;
}) {
  const { t } = useLingui();
  const now = useNow(work.running && !work.paused);
  const working = useConversationWaiting(botId) ?? t`Working`;
  if (!work.running) return null;
  return (
    <span
      data-testid="working-pill"
      className="relative flex animate-in items-center gap-2 rounded-full border border-separator bg-glass py-1 ps-[5px] pe-[11px] text-[12.5px] text-foreground tabular-nums fade-in slide-in-from-top-1 duration-400 motion-reduce:animate-none"
    >
      <span aria-hidden="true" className="sc-live-ring" />
      <SuperchatOrb size={16} state="working" flies={false} />
      <span data-testid="working-pill-text">
        {/* Pause holds every running Helper: the clock stops, the word is the strip's. */}
        {work.paused
          ? t`Paused`
          : work.since === null
            ? working
            : `${working} · ${formatClock(serverNow(now) - work.since)}`}
      </span>
    </span>
  );
}
