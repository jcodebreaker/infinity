import { useLingui } from "@lingui/react/macro";
import type { TopicCovered, TopicOpen, TopicSources } from "@superchat/contracts";
import { cn } from "@superchat/ui-web";
import { FileText, Mail, MessageCircle } from "lucide-react";
import { useTopicWords } from "./useTopics";

type Kind = TopicCovered["kind"] | TopicOpen["kind"];

/** Each kind's colour: decisions blue, things made green, findings teal, open items amber,
 * promises indigo; questions stay neutral. */
const KIND_TONE: Record<Kind, string> = {
  Decided: "text-tint bg-tint/10",
  Made: "text-ok bg-ok/15",
  Found: "text-tint-2 bg-tint-2/15",
  Asked: "text-ink-2 bg-foreground/8",
  Open: "text-warn bg-warn/15",
  Promised: "text-tint-3 bg-tint-3/15",
};

/** The dot colour a "What we covered" line carries on its timeline. */
export const KIND_DOT: Record<Kind, string> = {
  Decided: "border-tint",
  Made: "border-ok",
  Found: "border-tint-2",
  Asked: "border-ink-3",
  Open: "border-warn",
  Promised: "border-tint-3",
};

export function KindTag({ kind }: { kind: Kind }) {
  const words = useTopicWords();
  return (
    <span
      className={cn(
        "rounded-[5px] px-1.5 py-0.5 text-[10.5px] font-bold tracking-[0.04em] uppercase",
        KIND_TONE[kind],
      )}
    >
      {words.kinds[kind]}
    </span>
  );
}

/** Seven bars, oldest first: the days of this week the topic was touched; today's is ringed. */
export function WeekStrip({ days }: { days: boolean[] }) {
  const { t } = useLingui();
  return (
    <span className="flex items-end gap-[3px]" title={t`Days touched this week`}>
      <span className="sr-only">{t`Days touched this week`}</span>
      {days.map((on, index) => (
        <span
          key={index}
          aria-hidden="true"
          className={cn(
            "w-[5px] rounded-[2px]",
            on ? "h-3 bg-tint" : "h-1.5 bg-foreground/12",
            on && index === days.length - 1 && "ring-2 ring-tint/20",
          )}
        />
      ))}
    </span>
  );
}

/** How many chats, files and emails the topic drew on; zero counts are left out. */
export function SourceCounts({ sources }: { sources: TopicSources }) {
  const { t } = useLingui();
  const rows = [
    { count: sources.chats ?? 0, label: t`chats`, Icon: MessageCircle },
    { count: sources.files ?? 0, label: t`files`, Icon: FileText },
    { count: sources.emails ?? 0, label: t`emails`, Icon: Mail },
  ].filter((row) => row.count > 0);
  return (
    <>
      {rows.map(({ count, label, Icon }) => (
        <span key={label} className="flex items-center gap-[3px] tabular-nums">
          <Icon aria-hidden="true" size={13} strokeWidth={1.7} />
          {count}
          <span className="sr-only">{label}</span>
        </span>
      ))}
    </>
  );
}
