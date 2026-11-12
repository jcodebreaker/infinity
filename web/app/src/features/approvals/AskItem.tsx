import { t } from "@lingui/core/macro";
import { Trans, useLingui } from "@lingui/react/macro";
import { ChatMarkdown } from "@superchat/chat-ui/web";
import type { Ask } from "@superchat/contracts";
import { cn, Input } from "@superchat/ui-web";
import { BookmarkPlus, ChevronRight, HelpCircle, ShieldCheck, Sparkles } from "lucide-react";
import { useState } from "react";
import { SuperchatTile } from "../../components/chrome/SuperchatTile";
import { DetailRows, PRIMARY_BUTTON, QUIET_BUTTON } from "../../components/ui";
import { formatRelativeTime } from "../../lib/relative-time";
import { askDecision } from "./askDecision";

const KIND_ICON = {
  approval: ShieldCheck,
  proposal: Sparkles,
  question: HelpCircle,
  blocked_task: HelpCircle,
  skill_offer: BookmarkPlus,
} as const;

/**
 * Splits `Key: value` lines (an approval's To / Subject / Body headers) into rows for
 * `DetailRows`; null when the detail doesn't look like that shape, so it renders as quiet
 * text instead (the old design notes, "Waiting on you").
 */
function parseDetailRows(detail: string): { label: string; value: string }[] | null {
  const lines = detail.split("\n").filter((line) => line.trim().length > 0);
  if (lines.length === 0) return null;
  const rows: { label: string; value: string }[] = [];
  for (const line of lines) {
    const match = /^([A-Za-z][A-Za-z ]{0,20}):\s(.*)$/.exec(line);
    if (!match?.[1] || match[2] === undefined) return null;
    rows.push({ label: match[1], value: match[2] });
  }
  return rows;
}

/**
 * Renders one open Ask (the old design notes, "Waiting on you") as a big decision card: a
 * 44px tile for its kind, the title, where it came from and when, what it would do, then the
 * choices (two as an equal pair, the primary in the accent on the right).
 * Shared by the Feed and the Waiting-on-you sheet, so an Ask looks and answers the same
 * everywhere (docs/glossary.md: answering anywhere closes it everywhere). Matches the look of
 * `AskCard.tsx` (the Conversation's own ask block), but reads the `Ask` view type instead
 * of a thread message block's shape.
 */
export function AskItem({
  ask,
  onAnswer,
  onOpenSource,
}: {
  ask: Ask;
  onAnswer: (value: string) => Promise<void>;
  /** Called when the person taps the source in the meta line; omit to leave it inert. */
  onOpenSource?: () => void;
}) {
  const { t } = useLingui();
  const [text, setText] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const submitting = pending !== null;

  const sourceLabel = ask.goalTitle ?? t`Conversation`;
  const metaText = t`From ${sourceLabel} · ${formatRelativeTime(ask.createdAt)}`;
  const Icon = KIND_ICON[ask.kind];
  const engineApproval = ask.approval !== undefined;
  const title = engineApproval
    ? ask.text
    : ask.kind === "approval"
      ? t`One yes before I send this`
      : ask.text;
  const subtitle = engineApproval
    ? ask.detail
    : ask.kind === "approval"
      ? ask.text
      : ask.kind === "proposal" || ask.kind === "skill_offer"
        ? ask.detail
        : undefined;
  const structuredDetail = ask.kind === "approval" && !engineApproval ? ask.detail : undefined;
  const detailRows = structuredDetail ? parseDetailRows(structuredDetail) : null;

  async function submit(value: string) {
    if (submitting) return;
    const trimmed = ask.input === "secret" ? value : value.trim();
    if (ask.input === "secret" ? trimmed.length === 0 : !trimmed) return;
    setPending(ask.input ? "text-answer" : trimmed);
    setError(null);
    try {
      await onAnswer(trimmed);
      setText("");
    } catch (err) {
      setError(err instanceof Error ? err.message : t`Could not submit this answer`);
    } finally {
      setPending(null);
    }
  }

  const tone = askDecision(ask, title).tone;
  const [primary, secondary, ...more] = ask.choices;
  // Two choices read as one decision: the second as the quiet button on the left, the first
  // (the Ask's own primary) in the accent on the right. More choices stack in order.
  const pair = !ask.input && primary && more.length === 0 ? { primary, secondary } : null;
  const choiceLabel = (choice: { id: string; label: string }) =>
    pending === choice.id ? <Trans>Sending…</Trans> : choice.label;

  return (
    <div data-testid="ask-item" className="superchat-card flex flex-col gap-3 p-4">
      <div className="flex items-start gap-3.5">
        <SuperchatTile tone={tone} size={44}>
          <Icon strokeWidth={2.2} />
        </SuperchatTile>
        <div className="flex min-w-0 flex-1 flex-col gap-0.5 pt-0.5">
          <h3 className="text-[16px] leading-[1.3] font-semibold tracking-[-0.1px] text-foreground">
            {title}
          </h3>
          {onOpenSource ? (
            <button
              type="button"
              onClick={onOpenSource}
              className="self-start text-start text-[12.5px] text-ink-3 transition-colors hover:text-foreground"
            >
              {metaText}
            </button>
          ) : (
            <p className="text-[12.5px] text-ink-3">{metaText}</p>
          )}
          {subtitle ? (
            <div className="mt-1 text-[13.5px] leading-[1.5] text-ink-2">
              <ChatMarkdown>{subtitle}</ChatMarkdown>
            </div>
          ) : null}
        </div>
      </div>

      {ask.approval?.alsoAsks?.length ? (
        <ul className="flex flex-col gap-1 text-[13.5px] leading-[1.5] text-ink-2">
          {ask.approval.alsoAsks.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      ) : null}

      {ask.approval?.arguments ? (
        <details className="group text-[12.5px] text-ink-3">
          <summary className="flex w-fit cursor-pointer list-none items-center gap-1 transition-colors hover:text-foreground [&::-webkit-details-marker]:hidden">
            <Trans>Details</Trans>
            <ChevronRight
              size={12}
              className="shrink-0 transition-transform group-open:rotate-90"
            />
          </summary>
          <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-xl bg-muted px-3.5 py-3 font-mono text-[12.5px] leading-[1.7] text-muted-foreground">
            {ask.approval.arguments}
          </pre>
        </details>
      ) : null}

      {structuredDetail ? (
        detailRows ? (
          <DetailRows rows={detailRows} />
        ) : (
          <p className="whitespace-pre-wrap text-[13.5px] leading-[1.6] text-ink-2">
            {structuredDetail}
          </p>
        )
      ) : null}

      {ask.input ? (
        <form
          className="flex gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            void submit(text);
          }}
        >
          <Input
            aria-label={t`Answer`}
            type={ask.input === "secret" ? "password" : "text"}
            autoComplete="off"
            spellCheck={ask.input !== "secret"}
            disabled={submitting}
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder={t`Type your answer`}
            className="h-[34px] rounded-xl"
          />
          <button
            type="submit"
            className={cn(PRIMARY_BUTTON, "shrink-0 px-4")}
            disabled={!text.trim() || submitting}
          >
            {submitting ? <Trans>Sending…</Trans> : <Trans>Send</Trans>}
          </button>
        </form>
      ) : pair ? (
        <div className={cn("grid gap-2", pair.secondary ? "grid-cols-2" : "grid-cols-1")}>
          {pair.secondary ? (
            <button
              type="button"
              className={QUIET_BUTTON}
              disabled={submitting}
              onClick={() => pair.secondary && void submit(pair.secondary.id)}
            >
              {choiceLabel(pair.secondary)}
            </button>
          ) : null}
          <button
            type="button"
            className={PRIMARY_BUTTON}
            disabled={submitting}
            onClick={() => void submit(pair.primary.id)}
          >
            {choiceLabel(pair.primary)}
          </button>
        </div>
      ) : (
        <div className="flex flex-wrap gap-2">
          {ask.choices.map((choice, index) => (
            <button
              key={choice.id}
              type="button"
              className={index === 0 ? PRIMARY_BUTTON : QUIET_BUTTON}
              disabled={submitting}
              onClick={() => void submit(choice.id)}
            >
              {choiceLabel(choice)}
            </button>
          ))}
        </div>
      )}

      {error ? <p className="text-[13px] text-destructive">{error}</p> : null}
    </div>
  );
}

type AskGroupKey = "approvals" | "questions" | "plans";

const ASK_GROUP_ORDER: AskGroupKey[] = ["approvals", "questions", "plans"];

function askGroupKey(kind: Ask["kind"]): AskGroupKey {
  if (kind === "approval") return "approvals";
  if (kind === "proposal" || kind === "skill_offer") return "plans";
  return "questions";
}

export type AskGroup = { label: string | null; asks: Ask[] };

/**
 * Splits open Asks into small plain-heading groups (Approvals, Questions, Plans) once
 * there are enough of them that a flat list gets hard to scan (the old design notes,
 * "Group Asks under small plain headings only when there are more than 4"). Below that,
 * a single ungrouped bucket (`label: null`) keeps the list flat.
 */
export function groupAsks(asks: Ask[]): AskGroup[] {
  if (asks.length <= 4) return [{ label: null, asks }];
  const labels: Record<AskGroupKey, string> = {
    approvals: t`Approvals`,
    questions: t`Questions`,
    plans: t`Plans`,
  };
  return ASK_GROUP_ORDER.map((key) => ({
    label: labels[key],
    asks: asks.filter((ask) => askGroupKey(ask.kind) === key),
  })).filter((group) => group.asks.length > 0);
}

/**
 * A plain list of open Asks with hairline dividers between rows, grouped under small
 * headings once there are more than a handful (`groupAsks`). Shared by the Waiting-on-you
 * sheet and the Feed's pinned Asks so both read the same way.
 */
export function AskList({
  asks,
  onAnswer,
}: {
  asks: Ask[];
  onAnswer: (ask: Ask, value: string) => Promise<void>;
}) {
  const groups = groupAsks(asks);
  return (
    <div className="flex flex-col gap-5">
      {groups.map((group) => (
        <div key={group.label ?? "asks"} className="flex flex-col gap-2">
          {group.label ? (
            <h4 className="px-1.5 text-[13px] font-semibold text-foreground">{group.label}</h4>
          ) : null}
          <div className="flex flex-col gap-3">
            {group.asks.map((ask) => (
              <AskItem key={ask.id} ask={ask} onAnswer={(value) => onAnswer(ask, value)} />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
