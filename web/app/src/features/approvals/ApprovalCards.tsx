import type { Ask } from "@superchat/contracts";
import { Check, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { AskItem } from "./AskItem";
import { useAsks } from "./useAsks";

/** A waiting approval is the one thing worth noticing quickly: it arrives on the family stream,
 * and while that stream is down these cards poll faster than the Waiting-on-you list does. */
const APPROVAL_POLL_MS = 4_000;

/**
 * The approvals waiting in the Conversation (with its Helpers), as cards at the end of its
 * transcript. Answering locks the card with the choice; the same approval is also in Waiting on
 * you and closes there too.
 */
export function ApprovalCards({ botId }: { botId: string }) {
  const { asks, answer } = useAsks(botId, { pollMs: APPROVAL_POLL_MS });
  const [locked, setLocked] = useState<{ ask: Ask; label: string }[]>([]);
  const open = asks.filter(
    (ask) => ask.approval && !locked.some((entry) => entry.ask.id === ask.id),
  );
  if (open.length === 0 && locked.length === 0) return null;

  return (
    <div
      data-testid="approval-cards"
      className="mx-auto flex w-full max-w-[700px] flex-col gap-3 px-4 pb-3"
    >
      {locked.map(({ ask, label }) => (
        <div
          key={ask.id}
          className="superchat-card flex items-center gap-3 px-4 py-3 text-[14px] text-ink-2"
        >
          <ShieldCheck size={15} strokeWidth={1.75} aria-hidden="true" />
          <span className="min-w-0 flex-1" dir="auto">
            {ask.text}
          </span>
          <span className="flex items-center gap-1 text-foreground">
            <Check size={14} strokeWidth={2.25} aria-hidden="true" />
            {label}
          </span>
        </div>
      ))}
      {open.map((ask) => (
        <div key={ask.id}>
          <AskItem
            ask={ask}
            onAnswer={async (value) => {
              const label = ask.choices.find((choice) => choice.id === value)?.label ?? value;
              setLocked((current) => [...current, { ask, label }]);
              try {
                await answer({ askId: ask.id, runId: ask.runId, answer: value });
              } catch (err) {
                setLocked((current) => current.filter((entry) => entry.ask.id !== ask.id));
                throw err;
              }
            }}
          />
        </div>
      ))}
    </div>
  );
}
