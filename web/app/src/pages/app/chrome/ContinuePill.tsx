import { useLingui } from "@lingui/react/macro";
import { ChevronDown } from "lucide-react";
import { useTopicWords } from "../../../features/topics";

/**
 * Home's floating "Continue the conversation" at the bottom centre: the Conversation's last
 * line and when it was written, and a chevron that nudges down. It slides the Conversation in.
 */
export function ContinuePill({
  line,
  at,
  onContinue,
}: {
  line: string;
  /** When the last message was written (ISO). */
  at: string;
  onContinue: () => void;
}) {
  const { t } = useLingui();
  const words = useTopicWords();
  const seconds = Date.parse(at) / 1000;
  const when = Number.isNaN(seconds) ? "" : words.when(seconds, new Date());
  return (
    <button
      type="button"
      data-testid="continue-conversation"
      onClick={onContinue}
      className="group/continue sc-glass pointer-events-auto flex max-w-full min-w-0 items-center gap-3 rounded-full py-2 ps-4 pe-2.5 text-start shadow-lg transition-[transform,box-shadow,border-color] duration-200 hover:border-tint/35 hover:shadow-xl focus-visible:outline-2 focus-visible:outline-ring motion-safe:hover:-translate-y-0.5"
    >
      <span aria-hidden="true" className="size-2 flex-none rounded-full bg-ok ring-3 ring-ok/20" />
      <span className="flex min-w-0 flex-col">
        <span className="text-[13.5px] font-semibold text-foreground">{t`Continue the conversation`}</span>
        <span className="max-w-[26ch] truncate text-[12px] text-ink-2 md:max-w-[46ch]" dir="auto">
          {when ? t`Last: ${line} · ${when}` : t`Last: ${line}`}
        </span>
      </span>
      <span
        aria-hidden="true"
        className="grid size-[30px] flex-none place-items-center rounded-full bg-foreground text-window"
      >
        <ChevronDown size={16} strokeWidth={2.2} className="motion-safe:animate-nudge" />
      </span>
    </button>
  );
}
