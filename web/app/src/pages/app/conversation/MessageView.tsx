import { Trans, useLingui } from "@lingui/react/macro";
import { ChatMarkdown } from "@superchat/chat-ui/web";
import type { ThreadMessage } from "@superchat/contracts";
import {
  ENGINE_ERROR_NOTE,
  isEngineErrorText,
  isToolActivityBlock,
  parseAttachmentReferences,
} from "@superchat/core";
import { cn } from "@superchat/ui-web";
import { Folder } from "lucide-react";
import { memo } from "react";
import { CloudAgentCard } from "../../../components/CloudAgentCard";
import { ComposerAttachmentChip } from "../../../components/ComposerAttachmentChip";
import { ReplyCardBlockView } from "../../../components/cards/ReplyCard";
import { WorkspaceAttachmentChip } from "../../../components/WorkspaceAttachmentChip";
import type { TurnStrip } from "../../../features/activity";
import { drawnKey, WorkStrip } from "../../../features/activity";
import { AskCard } from "../../../features/approvals";
import { ArtifactFileCard } from "../../../features/artifacts";
import type { ArtifactTarget } from "../../../lib/artifact-open";
import { splitComposerAttachments } from "../../../lib/composer-attachments";
import { FirstRunHint } from "../intro";
import {
  FailureNote,
  InfoNote,
  useFailureNoteAction,
  useFailureNoteText,
  useInfoNoteText,
} from "./FailureNote";
import { FoldedText } from "./FoldedText";
import { MessageArtifactChip } from "./MessageArtifactChip";
import { ArtifactImage } from "./message-cards";

/** Muse mode only: one quiet line for a tool-activity block (the old design notes "Conversation"). */
function ToolActivityStep({ block }: { block: ThreadMessage["blocks"][number] }) {
  const label =
    block.kind === "steps"
      ? block.steps.map((step) => step.label).join(" · ")
      : "text" in block
        ? block.text
        : undefined;
  if (!label) return null;
  return (
    <div className="flex items-center gap-1.5 py-0.5 text-[12.5px] text-muted-foreground/70">
      <span aria-hidden="true" className="size-1 shrink-0 rounded-full bg-muted-foreground/50" />
      <span className="truncate" dir="auto">
        {label}
      </span>
    </div>
  );
}

const NOTHING_DRAWN: ReadonlySet<string> = new Set();

export const MessageView = memo(function MessageView({
  botDisplayName,
  artifactTarget,
  canAnswer,
  message,
  onAnswer,
  strip = null,
  drawn = NOTHING_DRAWN,
}: {
  /** The Muse's own name, for first-run hint copy (`FirstRunHint`). */
  botDisplayName?: string;
  artifactTarget: ArtifactTarget;
  canAnswer: boolean;
  message: ThreadMessage;
  onAnswer: (message: ThreadMessage, text: string, username?: string) => Promise<void>;
  /** The reply's work strip, when this message is where it sits (features/activity
   * `useTurnStrips`). */
  strip?: TurnStrip | null;
  /** The blocks the strips draw instead (`<messageId>:<index>`): the living plan, the Helpers. */
  drawn?: ReadonlySet<string>;
}) {
  const { t } = useLingui();
  const errorNote = useFailureNoteText();
  const errorAction = useFailureNoteAction();
  const infoNote = useInfoNoteText();
  const drawnByStrip = (index: number) => drawn.has(drawnKey(message.id, index));
  // The reply's work strip (ADR 0022, ADR 0023) sits under its words.
  const trailingStrip = strip ? (
    <WorkStrip
      botId={artifactTarget.botId}
      helpers={strip.helpers}
      turn={strip.turn}
      lit={strip.lit}
    />
  ) : null;
  const isNarration =
    message.role === "bot" &&
    message.blocks.length > 0 &&
    message.blocks.every(
      (block) => block.kind === "text" || block.kind === "progress" || block.kind === "steps",
    );
  const quoteMessageId = message.id.includes(":") ? undefined : message.id;
  const firstTextBlock = message.blocks.findIndex((block) => block.kind === "text");
  if (isNarration) {
    if (message.blocks.length === 0) return null;
    return (
      <>
        <div className="flex w-fit max-w-full justify-start">
          <div
            data-testid="message-bot-bubble"
            className={cn(
              "max-w-full space-y-2.5",
              "muse-reply text-[15px] leading-[1.47] tracking-[-0.24px] text-foreground",
            )}
            dir="auto"
          >
            {message.blocks.map((block, i) => {
              if (block.kind === "text" || (block.kind === "progress" && !block.activity)) {
                return (
                  <div
                    key={i}
                    data-quote-message-id={block.kind === "text" ? quoteMessageId : undefined}
                  >
                    <ChatMarkdown streaming={block.kind === "progress"}>{block.text}</ChatMarkdown>
                  </div>
                );
              }
              if (isToolActivityBlock(block)) {
                return <ToolActivityStep key={i} block={block} />;
              }
              return null;
            })}
          </div>
        </div>
        {trailingStrip}
      </>
    );
  }
  return (
    <>
      {message.blocks.map((block, i) => {
        if (isToolActivityBlock(block)) {
          return <ToolActivityStep key={i} block={block} />;
        }
        if (block.kind === "meta") {
          return (
            <div
              key={i}
              className="flex items-center justify-center gap-2 py-1 text-[13.5px] text-muted-foreground"
            >
              <span className="text-warning">◷</span>
              <span>{block.text}</span>
            </div>
          );
        }
        if (block.kind === "progress") {
          return (
            <div key={i} className="flex w-fit max-w-full justify-start">
              <div
                data-testid="message-bot-bubble"
                className={cn(
                  "max-w-full",
                  "muse-reply text-[15px] leading-[1.47] tracking-[-0.24px] text-foreground",
                )}
                dir="auto"
              >
                <ChatMarkdown streaming>{block.text}</ChatMarkdown>
              </div>
            </div>
          );
        }
        if (block.kind === "subagent") {
          const running = block.status === "running";
          const failed = block.status === "failed";
          return (
            <div
              key={i}
              className="w-[min(420px,90%)] rounded-[18px] border border-border bg-muted px-[18px] py-4"
            >
              <div className="flex items-center justify-between gap-3">
                <span className="text-[15px] font-medium text-foreground" dir="auto">
                  {block.name}
                </span>
                <span
                  className={`rounded-full px-[11px] py-1 text-[13px] ${
                    failed
                      ? "bg-destructive/15 text-destructive"
                      : running
                        ? "bg-warning/15 text-warning"
                        : "bg-success/15 text-success"
                  }`}
                  style={{
                    animation: running ? "rkPulse 1.2s ease-in-out infinite" : undefined,
                  }}
                >
                  {running ? <Trans>subagent</Trans> : block.status}
                </span>
              </div>
              <div className="mt-2 text-[13.5px] text-muted-foreground">{block.task}</div>
              {block.progress || block.result ? (
                <div className="mt-2.5 text-[14.5px] leading-[1.5] text-foreground/75">
                  <ChatMarkdown streaming={running}>
                    {block.result || block.progress || ""}
                  </ChatMarkdown>
                </div>
              ) : null}
            </div>
          );
        }
        if (block.kind === "image") {
          return (
            <div
              key={i}
              className={`flex ${message.role === "user" ? "justify-end" : "justify-start"}`}
            >
              <ArtifactImage
                artifactId={block.artifactId}
                name={block.name}
                mimeType={block.mimeType}
              />
            </div>
          );
        }
        if (block.kind === "file") {
          return (
            <div
              key={i}
              className={`flex ${message.role === "user" ? "justify-end" : "justify-start"}`}
            >
              <ArtifactFileCard
                artifactId={block.artifactId}
                name={block.name}
                mimeType={block.mimeType}
                size={block.size}
              />
            </div>
          );
        }
        if (block.kind === "error" && block.level === "info") {
          const note = infoNote(block.code);
          return note ? <InfoNote key={i}>{note}</InfoNote> : null;
        }
        if (block.kind === "error") {
          return (
            <FailureNote key={i} action={errorAction(block.code)}>
              {errorNote(block.code)}
            </FailureNote>
          );
        }
        if (block.kind === "text" && message.role === "user") {
          const files = parseAttachmentReferences(block.text);
          const ask = splitComposerAttachments(files.caption);
          const hasChips =
            ask.chips.length > 0 || files.attachments.length > 0 || files.folders.length > 0;
          const body = hasChips ? ask.rest : block.text;
          const fileBotId = artifactTarget.botId;
          return (
            <div key={i} className="flex w-fit max-w-full flex-col items-end gap-1.5">
              {files.folders.map((folder) => (
                <span
                  key={`folder:${folder}`}
                  data-testid="message-folder"
                  className="inline-flex max-w-full items-center gap-2 rounded-full border border-tint-ink/40 bg-tint-ink/10 px-3 py-1.5 text-[13px] text-foreground"
                >
                  <Folder
                    size={14}
                    strokeWidth={1.8}
                    aria-hidden="true"
                    className="shrink-0 text-tint-ink"
                  />
                  <span className="truncate" dir="auto">
                    {folder || t`Whole workspace`}
                  </span>
                </span>
              ))}
              {files.attachments.map((attachment) => (
                <WorkspaceAttachmentChip
                  key={attachment.path}
                  botId={fileBotId}
                  attachment={attachment}
                />
              ))}
              {ask.chips.map((chip) => (
                <ComposerAttachmentChip
                  key={chip.kind}
                  kind={chip.kind}
                  label={chip.label}
                  testId={`message-${chip.kind}-ask`}
                />
              ))}
              {/* One chip per message: an element ask already says which artifact, and more. */}
              {message.artifact && ask.chips.length === 0 && i === firstTextBlock ? (
                <MessageArtifactChip
                  botId={artifactTarget.botId}
                  itemId={message.id}
                  artifact={message.artifact}
                />
              ) : null}
              {body || !hasChips ? (
                <div
                  data-testid="message-user-bubble"
                  data-quote-message-id={quoteMessageId}
                  className={cn(
                    "max-w-full whitespace-pre-wrap wrap-anywhere",
                    "sc-user-bubble rounded-2xl px-4 py-3 text-[15px] leading-[1.6] text-foreground",
                  )}
                  dir="auto"
                >
                  <FoldedText text={body} />
                </div>
              ) : null}
              {message.delivered === "queued" ? (
                <span
                  data-testid="message-queued-note"
                  className="text-[12px] leading-4 text-muted-foreground"
                  dir="auto"
                >
                  <Trans>superchat will see this after the current step</Trans>
                </span>
              ) : null}
            </div>
          );
        }
        if (block.kind === "text" && isEngineErrorText(block.text)) {
          return <FailureNote key={i}>{ENGINE_ERROR_NOTE}</FailureNote>;
        }
        if (block.kind === "text") {
          return (
            <div key={i} className="flex w-fit max-w-full justify-start">
              <div
                data-testid="message-bot-bubble"
                className={cn(
                  "max-w-full",
                  "muse-reply text-[15px] leading-[1.47] tracking-[-0.24px] text-foreground",
                )}
                dir="auto"
              >
                <div data-quote-message-id={quoteMessageId}>
                  <ChatMarkdown>{block.text}</ChatMarkdown>
                </div>
              </div>
            </div>
          );
        }
        if (block.kind === "card") {
          return (
            <div key={i} className="flex justify-start">
              <div className="flex flex-col gap-2 rounded-[20px] bg-muted px-5 py-4">
                {block.lines.map((line) => (
                  <div key={line.k} className="flex items-baseline gap-2.5 text-[15px]">
                    <span className="text-success">✓</span>
                    <span className="font-semibold text-white">{line.k}</span>
                    <span className="text-muted-foreground">→</span>
                    <span>{line.v}</span>
                  </div>
                ))}
              </div>
            </div>
          );
        }
        if (block.kind === "ask") {
          const isProposal = block.actions?.some((action) => action.id === "accept") ?? false;
          if (!isProposal) {
            return (
              <AskCard
                key={i}
                block={block}
                canAnswer={canAnswer}
                onAnswer={(text, username) => onAnswer(message, text, username)}
              />
            );
          }
          return (
            <FirstRunHint
              key={i}
              hintKey="proposal-card"
              text={t`Start it, and ${botDisplayName ?? "superchat"} works on it in the background.`}
            >
              <AskCard
                block={block}
                canAnswer={canAnswer}
                onAnswer={(text, username) => onAnswer(message, text, username)}
              />
            </FirstRunHint>
          );
        }
        if (block.kind === "reply_card") {
          // The reply's living plan is drawn inside its work strip instead.
          if (drawnByStrip(i)) return null;
          return <ReplyCardBlockView key={i} block={block} messageId={message.id} index={i} />;
        }
        if (block.kind === "cloud_agent") return <CloudAgentCard key={i} block={block} />;
        // The Helpers this reply started are drawn by its work strip.
        return null;
      })}
      {trailingStrip}
    </>
  );
});
