import { useLingui } from "@lingui/react/macro";
import type { AnsweredAsk, Ask } from "@superchat/contracts";
import { Check, Minus } from "lucide-react";
import { useState } from "react";
import { DockSheet, DockSheetHeader } from "../../components/chrome/DockSheet";
import { SuperchatTile } from "../../components/chrome/SuperchatTile";
import { EmptyState, MUSE_INSET_GROUP, MUSE_LIST_ROW, SegmentedControl } from "../../components/ui";
import { CardSkeletonList } from "../../components/ui/CardSkeleton";
import { AskList, useAsks } from ".";
import { useAskHistory } from "./useAskHistory";

/** An Ask answered from this sheet, kept for the "Decided" list while the app is open. */
type Decided = { ask: Ask; label: string; primary: boolean; at: Date };

type InboxTab = "all" | "approvals" | "questions" | "history";

/**
 * The Inbox: every open Ask, newest first, as decision cards answerable in place (approvals
 * with their choices, questions with theirs), then what was decided here this session. Tabs
 * split All / Approvals / Questions; History
 * lists what was decided in the last 30 days, by whom and when, from the engine's record (ADR
 * 0036). The dock's dot and this list share `useAsks`, so answering here (or in the
 * Conversation) closes the Ask everywhere at once (docs/glossary.md, "Ask").
 */
export function WaitingSheet({
  botId,
  open,
  onOpenChange,
}: {
  botId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t, i18n } = useLingui();
  const { asks, loading, answer } = useAsks(botId);
  const [decided, setDecided] = useState<Decided[]>([]);

  async function handleAnswer(ask: Ask, value: string) {
    await answer({ askId: ask.id, runId: ask.runId, answer: value });
    const index = ask.choices.findIndex((choice) => choice.id === value);
    setDecided((current) => [
      {
        ask,
        label: index >= 0 ? (ask.choices[index]?.label ?? value) : t`Answered`,
        primary: index <= 0,
        at: new Date(),
      },
      ...current,
    ]);
  }

  const approvalTitle = t`One yes before I send this`;
  const [tab, setTab] = useState<InboxTab>("all");
  const { history, failed } = useAskHistory(
    botId,
    open && tab === "history",
    asks.map((ask) => ask.id).join(" "),
  );
  const decisionLabel: Record<AnsweredAsk["decision"], string> = {
    approved: t`Allowed`,
    denied: t`Denied`,
    accepted: t`Accepted`,
    dismissed: t`Dismissed`,
  };

  const shown = asks.filter((ask) =>
    tab === "all" ? true : tab === "approvals" ? ask.kind === "approval" : ask.kind !== "approval",
  );
  const approvalCount = asks.filter((ask) => ask.kind === "approval").length;
  const count = (n: number) => <span className="ms-1 text-ink-3 tabular-nums">{n}</span>;

  return (
    <DockSheet open={open} title={t`Inbox`} onClose={() => onOpenChange(false)}>
      <DockSheetHeader title={t`Inbox`} subtitle={t`I ask before doing anything I can't undo.`}>
        <SegmentedControl
          label={t`Inbox filter`}
          className="shrink-0 [&>button]:flex-none"
          value={tab}
          onChange={setTab}
          options={[
            {
              value: "all",
              label: (
                <>
                  {t`All`}
                  {count(asks.length)}
                </>
              ),
            },
            {
              value: "approvals",
              label: (
                <>
                  {t`Approvals`}
                  {count(approvalCount)}
                </>
              ),
            },
            {
              value: "questions",
              label: (
                <>
                  {t`Questions`}
                  {count(asks.length - approvalCount)}
                </>
              ),
            },
            { value: "history", label: t`History` },
          ]}
        />
      </DockSheetHeader>
      <div className="sc-scroll flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto">
        {tab === "history" ? (
          <div data-testid="waiting-history">
            {history === null && !failed ? (
              <CardSkeletonList count={2} />
            ) : failed && history === null ? (
              <p className="px-1.5 text-[13px] text-ink-3">{t`Couldn't load`}</p>
            ) : history?.length === 0 ? (
              <EmptyState illustration="bell" headline={t`Nothing decided yet`}>
                {t`What you decide shows up here for 30 days.`}
              </EmptyState>
            ) : (
              <div className={MUSE_INSET_GROUP}>
                {history?.map((entry) => {
                  const yes = entry.decision === "approved" || entry.decision === "accepted";
                  const when = new Date(entry.decidedAt).toLocaleString(i18n.locale || "en", {
                    month: "short",
                    day: "numeric",
                    hour: "numeric",
                    minute: "2-digit",
                  });
                  return (
                    <div key={entry.id} className={MUSE_LIST_ROW}>
                      <SuperchatTile tone={yes ? "green" : "gray"} size={28}>
                        {yes ? <Check strokeWidth={2.6} /> : <Minus strokeWidth={2.6} />}
                      </SuperchatTile>
                      <span className="min-w-0">
                        <span className="block truncate text-foreground" dir="auto">
                          {entry.title}
                        </span>
                        <span className="block truncate text-[12px] text-ink-3">
                          {entry.byYou || !entry.decidedBy
                            ? t`You · ${when}`
                            : `${entry.decidedBy} · ${when}`}
                        </span>
                      </span>
                      <span className="max-w-[140px] truncate text-[13px] text-ink-3">
                        {decisionLabel[entry.decision]}
                      </span>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        ) : (
          <>
            <div data-testid="waiting-open">
              {asks.length === 0 && loading ? (
                <CardSkeletonList count={2} />
              ) : shown.length === 0 ? (
                <EmptyState illustration="bell" headline={t`You're all caught up`}>
                  {t`When I need a decision or an answer, it'll show up here.`}
                </EmptyState>
              ) : (
                <AskList asks={shown} onAnswer={handleAnswer} />
              )}
            </div>
            {decided.length > 0 ? (
              <section className="flex flex-col gap-1.5" data-testid="waiting-decided">
                <h3 className="px-1.5 text-[13px] font-semibold text-foreground">{t`Decided`}</h3>
                <div className={MUSE_INSET_GROUP}>
                  {decided.map((entry) => (
                    <div key={entry.ask.id} className={MUSE_LIST_ROW}>
                      <SuperchatTile tone={entry.primary ? "green" : "gray"} size={28}>
                        {entry.primary ? <Check strokeWidth={2.6} /> : <Minus strokeWidth={2.6} />}
                      </SuperchatTile>
                      <span className="min-w-0">
                        <span className="block truncate text-foreground" dir="auto">
                          {entry.ask.kind === "approval" && !entry.ask.approval
                            ? approvalTitle
                            : entry.ask.text}
                        </span>
                        <span className="block text-[12px] text-ink-3">
                          {entry.at.toLocaleTimeString(i18n.locale || "en", {
                            hour: "numeric",
                            minute: "2-digit",
                          })}
                        </span>
                      </span>
                      <span className="max-w-[140px] truncate text-[13px] text-ink-3">
                        {entry.label}
                      </span>
                    </div>
                  ))}
                </div>
              </section>
            ) : null}
          </>
        )}
      </div>
    </DockSheet>
  );
}
