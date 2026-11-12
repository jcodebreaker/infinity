import { useLingui } from "@lingui/react/macro";
import type { Ask } from "@superchat/contracts";
import { cn } from "@superchat/ui-web";
import { BookmarkPlus, HelpCircle, ShieldCheck, Sparkles } from "lucide-react";
import { useState } from "react";
import { SuperchatTile } from "../../components/chrome/SuperchatTile";
import { askDecision } from "./askDecision";

const KIND_ICON = {
  approval: ShieldCheck,
  proposal: Sparkles,
  question: HelpCircle,
  blocked_task: HelpCircle,
  skill_offer: BookmarkPlus,
} as const;

const BUTTON =
  "h-[34px] min-w-0 truncate rounded-xl px-3 text-[14px] font-medium transition-[background-color,filter] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:opacity-60";

/**
 * One open Ask as a decision card (the old design notes "Waiting on you"): a colored tile, the
 * title and a quiet line, then two equal buttons, the secondary choice on the left and the
 * primary in the accent on the right. An Ask that needs typing, or has more choices, offers
 * one button that opens Waiting on you instead.
 */
export function AskDecisionCard({
  ask,
  onAnswer,
  onOpenWaiting,
}: {
  ask: Ask;
  onAnswer: (choiceId: string) => Promise<void>;
  onOpenWaiting: () => void;
}) {
  const { t } = useLingui();
  const decision = askDecision(ask, t`One yes before I send this`);
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const Icon = KIND_ICON[ask.kind];

  async function choose(id: string) {
    if (pending) return;
    setPending(id);
    setError(null);
    try {
      await onAnswer(id);
    } catch (err) {
      setError(err instanceof Error ? err.message : t`Could not submit this answer`);
    } finally {
      setPending(null);
    }
  }

  const { actions } = decision;
  return (
    <div data-testid="ask-decision" className="superchat-group flex flex-col gap-1.5 p-3.5">
      <div className="flex items-start gap-2.5">
        <SuperchatTile tone={decision.tone} size={28}>
          <Icon strokeWidth={2.4} />
        </SuperchatTile>
        <div className="min-w-0 flex-1">
          <p
            className="line-clamp-3 text-[14px] leading-[1.3] font-semibold text-foreground"
            dir="auto"
          >
            {decision.title}
          </p>
          {decision.subtitle ? (
            <p className="mt-0.5 line-clamp-2 text-[12px] leading-[1.35] text-ink-3" dir="auto">
              {decision.subtitle}
            </p>
          ) : null}
        </div>
      </div>
      <div className={cn("mt-2 grid gap-2", actions?.secondary ? "grid-cols-2" : "grid-cols-1")}>
        {actions ? (
          <>
            {actions.secondary ? (
              <button
                type="button"
                disabled={pending !== null}
                onClick={() => actions.secondary && void choose(actions.secondary.id)}
                className={cn(BUTTON, "bg-selection text-foreground hover:brightness-95")}
              >
                {pending === actions.secondary.id ? t`Sending…` : actions.secondary.label}
              </button>
            ) : null}
            <button
              type="button"
              disabled={pending !== null}
              onClick={() => void choose(actions.primary.id)}
              className={cn(BUTTON, "bg-tint text-white hover:brightness-110")}
            >
              {pending === actions.primary.id ? t`Sending…` : actions.primary.label}
            </button>
          </>
        ) : (
          <button
            type="button"
            onClick={onOpenWaiting}
            className={cn(BUTTON, "bg-tint text-white hover:brightness-110")}
          >
            {t`Answer`}
          </button>
        )}
      </div>
      {error ? <p className="text-[12px] text-destructive">{error}</p> : null}
    </div>
  );
}
