import { ChatMarkdown } from "@superchat/chat-ui/web";
import type { ReplyCardBlock } from "@superchat/contracts";
import { parseReplyCard } from "@superchat/contracts";
import { ReplyArtifactFileCard } from "../../features/artifacts";
import { LegacyChartNote } from "../../features/charts";
import { PassageChips } from "../../features/knowledge";
import { TopicCard } from "../../features/topics";
import { CardSkeleton, catalog } from "./catalog";
import {
  useFileStudio,
  useReplyCardDraft,
  useReplyCardRunning,
  useReplyCardSend,
  useResolvedReplyCard,
} from "./context";
import { SecureEntryCard } from "./SecureEntryCard";

/** The markdown the Muse wrote for a card the client cannot draw. */
function Fallback({ text }: { text: string }) {
  return (
    <div className="max-w-full text-[16px] leading-[1.65] text-foreground" dir="auto">
      <ChatMarkdown>{text}</ChatMarkdown>
    </div>
  );
}

/** One `reply_card` block: skeleton while pending, the catalog component when it parses,
 * otherwise its markdown fallback. `answer` is the person's reply to it, when there is one;
 * `cardId` (the engine's card id) names it in an ask card's structured answer. */
export function ReplyCard({
  block,
  answer,
  cardId,
}: {
  block: ReplyCardBlock;
  answer?: string;
  cardId?: string;
}) {
  const draft = useReplyCardDraft();
  const send = useReplyCardSend();
  const studio = useFileStudio();
  if (block.pending) return <CardSkeleton card={block.card} />;
  // The chart card was retired; old transcripts still hold it.
  if (block.card === "chart") return <LegacyChartNote />;
  const parsed = parseReplyCard(block);
  if (!parsed) return <Fallback text={block.fallback} />;
  const title = block.title;
  switch (parsed.kind) {
    case "sources":
      return <catalog.sources title={title} data={parsed.data} />;
    case "compare":
      return <catalog.compare title={title} data={parsed.data} />;
    case "ask":
      return <catalog.ask title={title} data={parsed.data} answer={answer} cardId={cardId} />;
    case "follow_ups":
      return <catalog.follow_ups data={parsed.data} />;
    case "quote":
      return <catalog.quote title={title} data={parsed.data} />;
    case "person":
      return <catalog.person title={title} data={parsed.data} />;
    case "file":
      return parsed.data.url ? (
        <catalog.file title={title} data={parsed.data} />
      ) : (
        <ReplyArtifactFileCard title={title} data={parsed.data} />
      );
    case "passages":
      return <PassageChips data={parsed.data} />;
    case "progress":
      return <catalog.progress title={title} data={parsed.data} />;
    case "secure_entry":
      return <SecureEntryCard data={parsed.data} />;
    case "topic":
      return (
        <TopicCard
          data={parsed.data}
          onDraft={draft ?? undefined}
          onSend={send ?? undefined}
          onOpenFile={studio ? (id, name) => studio.open(id, name) : undefined}
        />
      );
  }
}

/** A reply card inside a transcript: honors in-place updates and the person's answer. */
export function ReplyCardBlockView({
  block,
  messageId,
  index,
}: {
  block: ReplyCardBlock;
  messageId: string;
  index: number;
}) {
  const resolved = useResolvedReplyCard(messageId, index);
  const running = useReplyCardRunning();
  const shown = resolved ? resolved.block : block;
  if (!shown) return null;
  // Chips are for the latest settled answer of a resolved thread; elsewhere (no thread to judge
  // "latest" by) or while the Muse works, they stay out of the way.
  if (shown.card === "follow_ups" && (!resolved || running)) return null;
  return (
    <div className="flex w-full justify-start py-1">
      <ReplyCard block={shown} answer={resolved?.answer} cardId={shown.id} />
    </div>
  );
}
