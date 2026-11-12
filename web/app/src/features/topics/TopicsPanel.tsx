import { plural } from "@lingui/core/macro";
import { useLingui } from "@lingui/react/macro";
import type { Topic } from "@superchat/contracts";
import { cn } from "@superchat/ui-web";
import { ArrowRight, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { topicsByDay } from "./topicTime";
import { useTopicWords } from "./useTopics";

/**
 * The Topics panel the dock's lightbulb opens: days newest first, one row per topic touched that
 * day, so one topic can show on several days. A row shows its latest moment and opens to all of
 * that day's moments, its last covered lines and first open item, with "Pick up in chat".
 */
export function TopicsPanel({
  open,
  topics,
  onClose,
  onPick,
}: {
  open: boolean;
  topics: readonly Topic[] | null;
  onClose: () => void;
  onPick: (topic: Topic) => void;
}) {
  const { t } = useLingui();
  const words = useTopicWords();
  const now = useMemo(() => new Date(), [topics, open]);
  const days = useMemo(() => topicsByDay(topics ?? [], now), [topics, now]);
  const [expanded, setExpanded] = useState<string | null>(null);
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  return (
    <aside
      data-testid="topics-panel"
      aria-label={t`Topics`}
      aria-hidden={!open}
      inert={!open || undefined}
      className={cn(
        "sc-glass absolute inset-x-4 top-[76px] bottom-[88px] z-40 flex flex-col rounded-[22px] shadow-2xl transition-[transform,opacity] duration-300 ease-[cubic-bezier(.2,.8,.2,1)] md:inset-x-auto md:start-[116px] md:bottom-3 md:w-[360px] md:max-w-[calc(100%-132px)]",
        open
          ? "opacity-100"
          : "pointer-events-none -translate-x-4 opacity-0 motion-reduce:translate-x-0",
      )}
    >
      <header className="flex items-center gap-2.5 px-4 pt-4 pb-2.5">
        <h2 className="flex-1 text-[17px] font-semibold tracking-[-0.01em]">{t`Topics`}</h2>
        <button
          type="button"
          aria-label={t`Close topics`}
          onClick={onClose}
          className="grid size-[30px] place-items-center rounded-[9px] text-ink-2 transition-colors hover:bg-foreground/10 focus-visible:outline-2 focus-visible:outline-ring"
        >
          <X size={18} strokeWidth={1.7} />
        </button>
      </header>
      <p className="mx-4 mb-2.5 text-[12.5px] leading-[1.45] text-ink-2">
        {t`What you worked on, newest first. Open one to see where you left off, then pick it up in the chat.`}
      </p>
      <div className="sc-scroll min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        {topics && days.length === 0 ? (
          <p className="px-2 py-3 text-[13px] text-ink-2">
            {t`Nothing yet. Topics appear here a little while after you work on something.`}
          </p>
        ) : null}
        {days.map((group) => (
          <section key={group.day}>
            <h3 className="sticky top-0 z-[1] bg-gradient-to-b from-glass from-70% to-transparent px-2 pt-2.5 pb-1.5 text-[11.5px] font-semibold tracking-[0.06em] text-ink-2 uppercase">
              {words.day(group.rows[0]?.moments[0]?.at ?? 0, now)}
            </h3>
            {group.rows.map(({ topic, moments }) => {
              const id = `${group.day}:${topic.key}`;
              const isOpen = expanded === id;
              const [latest] = moments;
              const more = moments.length - 1;
              const recent = topic.covered.slice(0, 2);
              const firstOpen = topic.open[0];
              return (
                <div
                  key={id}
                  className={cn("rounded-[14px] transition-colors", isOpen && "bg-tint/6")}
                >
                  <button
                    type="button"
                    aria-expanded={isOpen}
                    onClick={() => setExpanded(isOpen ? null : id)}
                    className="grid w-full grid-cols-[44px_minmax(0,1fr)] gap-2.5 rounded-[14px] px-2 py-[9px] text-start transition-colors hover:bg-tint/8 focus-visible:outline-2 focus-visible:outline-ring"
                  >
                    <span className="pt-px text-[12px] text-ink-2 tabular-nums">
                      {words.time(latest.at)}
                    </span>
                    <span>
                      <span className="flex flex-wrap items-center gap-1.5 text-[14px] font-semibold tracking-[-0.005em]">
                        <span dir="auto">{topic.title}</span>
                        {moments.some((moment) => moment.fresh) ? (
                          <span className="rounded-[5px] bg-ok/15 px-1.5 py-0.5 text-[10.5px] font-bold tracking-[0.04em] text-ok uppercase">
                            {t`New`}
                          </span>
                        ) : null}
                      </span>
                      <span
                        className="mt-0.5 block text-[12.5px] leading-[1.4] text-ink-2"
                        dir="auto"
                      >
                        {latest.line}
                        {more > 0 ? (
                          <span className="ms-1.5 whitespace-nowrap text-ink-3">
                            {plural(more, { one: "+# more", other: "+# more" })}
                          </span>
                        ) : null}
                      </span>
                    </span>
                  </button>
                  <div
                    className={cn(
                      "grid transition-[grid-template-rows] duration-300 ease-[cubic-bezier(.2,.8,.2,1)]",
                      isOpen ? "grid-rows-[1fr]" : "grid-rows-[0fr]",
                    )}
                  >
                    <div className="overflow-hidden" inert={!isOpen || undefined}>
                      <div className="flex flex-col gap-2 ps-[62px] pe-2.5 pb-3">
                        {more > 0 ? (
                          <ol className="m-0 flex list-none flex-col gap-1 p-0 text-[12.5px] leading-[1.4]">
                            {moments.map((moment) => (
                              <li key={`${moment.at}:${moment.line}`} className="flex gap-2">
                                <span className="shrink-0 text-ink-2 tabular-nums">
                                  {words.time(moment.at)}
                                </span>
                                <span aria-hidden="true" className="text-ink-3">
                                  ·
                                </span>
                                <span dir="auto">{moment.line}</span>
                              </li>
                            ))}
                          </ol>
                        ) : null}
                        {recent.length || firstOpen ? (
                          <ul className="m-0 list-disc ps-4 text-[12.5px] leading-normal">
                            {recent.map((line) => (
                              <li key={`${line.date}:${line.text}`} dir="auto">
                                {line.text}
                              </li>
                            ))}
                            {firstOpen ? (
                              <li dir="auto">
                                <b>{words.kinds[firstOpen.kind]}:</b> {firstOpen.text}
                                {firstOpen.due ? (
                                  <span className="text-ink-2 tabular-nums">
                                    {" · "}
                                    {words.dueDay(firstOpen.due, now)}
                                  </span>
                                ) : null}
                              </li>
                            ) : null}
                          </ul>
                        ) : null}
                        <button
                          type="button"
                          onClick={() => onPick(topic)}
                          className="inline-flex items-center gap-1.5 self-start rounded-full bg-foreground px-3 py-[7px] text-[12.5px] font-semibold text-background transition-colors hover:bg-tint hover:text-white focus-visible:outline-2 focus-visible:outline-ring"
                        >
                          {t`Pick up in chat`}
                          <ArrowRight aria-hidden="true" size={13} strokeWidth={2} />
                        </button>
                      </div>
                    </div>
                  </div>
                </div>
              );
            })}
          </section>
        ))}
      </div>
    </aside>
  );
}
