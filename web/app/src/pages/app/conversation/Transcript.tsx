import { useLingui } from "@lingui/react/macro";
import type { ThreadMessage } from "@superchat/contracts";
import { isToolActivityBlock } from "@superchat/core";
import { cn } from "@superchat/ui-web";
import { ArrowDown } from "lucide-react";
import type { ComponentProps, ReactNode, RefObject } from "react";
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { CardAnswer } from "../../../components/cards/context";
import {
  ReplyCardBotProvider,
  ReplyCardDraftProvider,
  ReplyCardSendProvider,
  ReplyCardThreadProvider,
} from "../../../components/cards/context";
import { hiddenFollowUpMessageIds } from "../../../components/cards/thread";
import type { TranscriptWork, WorkState } from "../../../features/activity";
import {
  AnsweredWhileWorking,
  drawnOnlyMessageIds,
  noteServerClock,
  RoundStrip,
  useAnsweredWhileWorking,
  useTurnStrips,
} from "../../../features/activity";
import { AwayDivider, ProactiveMeta, useAwayMarker, useOutboxSeen } from "../../../features/outbox";
import type { ArtifactTarget } from "../../../lib/artifact-open";
import { quoteDraftForSelection } from "../../../lib/quote-selection";
import { transcriptIsNearEnd, transcriptMovedDown } from "../../../lib/transcript-scroll";
import type { MuseLiveRun } from "../../../lib/useMuseLiveState";
import { useMuseLiveState } from "../../../lib/useMuseLiveState";
import { FailureRun } from "./FailureNote";
import type { TranscriptRow } from "./failureNotes";
import { foldFailureRuns } from "./failureNotes";
import { MessageHoverActions } from "./MessageHoverActions";
import { MessageView } from "./MessageView";
import { hasOpenMuseAsk } from "./messageText";
import { QuoteSelectionButton } from "./QuoteSelectionButton";
import { UserMessageMeta } from "./UserMessageMeta";
import { WorkingRow } from "./WorkingRow";

/** The transcript, with its reply cards resolved across the whole thread (in-place updates,
 * answered asks). */
export function Transcript({
  onSendCard,
  onDraftCard,
  ...props
}: ComponentProps<typeof TranscriptView> & {
  /** A reply card's button sends this text as the person's next message (an ask card's pick
   * with its structured answer). */
  onSendCard?: (text: string, answer?: CardAnswer) => void;
  /** A card puts this text in the composer, unsent (a picked-up topic's starters). */
  onDraftCard?: (text: string) => void;
}) {
  // Follow-up chips that are not shown leave no row behind (no ghost bubble or Copy action).
  const { messages: allMessages } = props;
  const running = props.workState.turnRunning;
  const shownMessages = useMemo(() => {
    const hidden = hiddenFollowUpMessageIds(allMessages, running);
    return hidden.size === 0 ? allMessages : allMessages.filter((m) => !hidden.has(m.id));
  }, [allMessages, running]);
  const thread = (
    <ReplyCardBotProvider botId={props.artifactTarget.botId}>
      <ReplyCardThreadProvider messages={props.messages} running={running}>
        <TranscriptView {...props} messages={shownMessages} />
      </ReplyCardThreadProvider>
    </ReplyCardBotProvider>
  );
  const view = onDraftCard ? (
    <ReplyCardDraftProvider draft={onDraftCard}>{thread}</ReplyCardDraftProvider>
  ) : (
    thread
  );
  return onSendCard ? (
    <ReplyCardSendProvider send={onSendCard}>{view}</ReplyCardSendProvider>
  ) : (
    view
  );
}

const TranscriptView = memo(function Transcript({
  museFace,
  botDisplayName,
  museRuns,
  leading,
  trailing,
  followSignal,
  scrollRef,
  scrollRequest,
  onScrollRequestHandled,
  artifactTarget,
  messages,
  loading,
  olderCursor,
  loadingOlder,
  work,
  workState,
  onLoadOlder,
  onShowEarlier,
  onAnswer,
  onReply,
  onQuote,
  shown = true,
}: {
  /** The face shown beside the Muse's replies. */
  museFace: { color: string; identity: string };
  /** The Muse's own name, for first-run hint copy (`FirstRunHint`). */
  botDisplayName?: string;
  /** The turn in flight, for the gutter face and bottom-of-transcript row. */
  museRuns: readonly MuseLiveRun[];
  /** Rendered at the top of the scrolling column, before the messages. */
  leading?: ReactNode;
  /** Rendered at the bottom of the scrolling column, after the messages. */
  trailing?: ReactNode;
  /** Changes when the person sends: follow the tail again. */
  followSignal?: number;
  scrollRef: RefObject<HTMLDivElement | null>;
  scrollRequest: { messageId: string; nonce: number } | null;
  onScrollRequestHandled: () => void;
  artifactTarget: ArtifactTarget;
  messages: ThreadMessage[];
  /** The transcript's first page has not arrived yet: a quiet loading line, never a blank. */
  loading?: boolean;
  olderCursor: number | string | null;
  loadingOlder: boolean;
  /** The transcript's work (features/activity `transcriptWork`), read once by the caller: each
   * reply's strip and the blocks it draws. */
  work: TranscriptWork;
  /** The Conversation's work state (features/activity `useWorkState`). */
  workState: WorkState;
  onLoadOlder: () => void | Promise<void>;
  /** The Conversation was cleared and its earlier history can be shown (the Muse only). */
  onShowEarlier?: () => void | Promise<void>;
  onAnswer: (message: ThreadMessage, text: string, username?: string) => Promise<void>;
  /** Without reply/quote handlers those actions are hidden. */
  onReply?: (message: ThreadMessage) => void;
  onQuote?: (message: ThreadMessage, quote: string) => void;
  /** The chat is in front (no sheet over it): reaching its end counts as seeing it (ADR 0036). */
  shown?: boolean;
}) {
  const { t } = useLingui();
  const running = workState.turnRunning;
  const { label: museLiveLabel } = useMuseLiveState({
    botId: museFace.identity,
    runs: museRuns,
    messages,
  });
  const [atEnd, setAtEnd] = useState(true);
  const following = useRef(true);
  const autoScrolling = useRef(false);
  const lastScrollTop = useRef<number | null>(null);
  const autoScrollTimer = useRef<number | undefined>(undefined);
  const jumpButtonRef = useRef<HTMLButtonElement>(null);
  // What Superchat said on its own while the person was away (ADR 0036): one divider above the
  // first of it, and it counts as seen once the chat is in front and at its end.
  const awayMarker = useAwayMarker(artifactTarget.botId, followSignal);
  useOutboxSeen({ botId: artifactTarget.botId, messages, atEnd, shown });
  const messageById = useMemo(
    () => new Map(messages.map((message) => [message.id, message])),
    [messages],
  );
  // A reply given while earlier Helpers still work says so, quietly (ADR 0022).
  const answeredWhileWorking = useAnsweredWhileWorking(artifactTarget.botId, messages, running);
  // Each reply's work strip (ADR 0023), handed to the message it sits under; a message holding
  // only what a strip draws leaves no row behind.
  const { strips, drawn } = useTurnStrips(work, workState, museLiveLabel);
  // Live clocks count against the engine's timestamps: learn its clock from what arrives.
  useEffect(() => noteServerClock(messages), [messages]);
  // The newest work has a strip: the Round the person's newest message opened, or a reply's.
  const lastTurnHasStrip = useMemo(
    () =>
      Boolean(messages.findLast((message) => message.role === "user")?.roundId) ||
      [...work.turns.values()].some((turn) => turn.last),
    [messages, work],
  );
  // Muse: failed replies in a row read as one "3 failed attempts" line (failureNotes.ts).
  const transcriptRows = useMemo<TranscriptRow[]>(() => {
    const drawnOnly = drawnOnlyMessageIds(messages, work);
    return foldFailureRuns(
      drawnOnly.size === 0 ? messages : messages.filter((message) => !drawnOnly.has(message.id)),
    );
  }, [messages, work]);
  const [quoteDraft, setQuoteDraft] = useState<{
    message: ThreadMessage;
    text: string;
    range: Range;
  } | null>(null);
  const selectingWithMouse = useRef(false);

  const evaluateSelection = useCallback(() => {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || selection.rangeCount === 0) {
      setQuoteDraft(null);
      return;
    }
    const range = selection.getRangeAt(0);
    const contentOf = (node: Node) =>
      (node instanceof Element ? node : node.parentElement)?.closest<HTMLElement>(
        "[data-quote-message-id]",
      ) ?? null;
    const startContent = contentOf(range.startContainer);
    const endContent = contentOf(range.endContainer);
    // selection.toString() serializes the whole range (a Ctrl+A transcript is
    // unbounded), so it only runs once both endpoints sit in one message.
    const draft = quoteDraftForSelection(
      {
        startContent,
        endContent,
        text: startContent && startContent === endContent ? selection.toString() : "",
      },
      messageById,
    );
    setQuoteDraft((prev) => {
      if (!draft) return null;
      // Repeat firings for an unchanged selection reuse the draft so the
      // transcript isn't re-rendered by every unrelated selection event. A
      // moved selection (e.g. keyboard-selecting a second occurrence of the
      // same text) must carry its new Range — the pill anchors to it.
      if (
        prev &&
        prev.message === draft.message &&
        prev.text === draft.text &&
        prev.range.compareBoundaryPoints(Range.START_TO_START, range) === 0 &&
        prev.range.compareBoundaryPoints(Range.END_TO_END, range) === 0
      ) {
        return prev;
      }
      return { ...draft, range };
    });
  }, [messageById]);

  // Keyboard and assistive-tech selections never reach a mouseup, so the pill
  // lifecycle listens on selectionchange; the mouse flag keeps it hidden while
  // a drag is still in flight.
  useEffect(() => {
    const onMouseDown = (event: MouseEvent) => {
      selectingWithMouse.current = true;
      if ((event.target as Element | null)?.closest?.("[data-quote-selection]")) return;
      setQuoteDraft(null);
    };
    const onMouseUp = () => {
      selectingWithMouse.current = false;
      evaluateSelection();
    };
    const onSelectionChange = () => {
      if (!selectingWithMouse.current) evaluateSelection();
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setQuoteDraft(null);
    };
    // A drag that ends outside the window never fires document mouseup; reset
    // the flag on blur so keyboard selections keep working afterwards.
    const onWindowBlur = () => {
      selectingWithMouse.current = false;
    };
    document.addEventListener("mousedown", onMouseDown, true);
    document.addEventListener("mouseup", onMouseUp, true);
    document.addEventListener("selectionchange", onSelectionChange);
    document.addEventListener("keydown", onKey);
    window.addEventListener("blur", onWindowBlur);
    return () => {
      document.removeEventListener("mousedown", onMouseDown, true);
      document.removeEventListener("mouseup", onMouseUp, true);
      document.removeEventListener("selectionchange", onSelectionChange);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("blur", onWindowBlur);
    };
  }, [evaluateSelection]);
  const snapToEnd = useCallback(() => {
    const element = scrollRef.current;
    if (!element) return;
    following.current = true;
    autoScrolling.current = false;
    setAtEnd(true);
    element.scrollTo({ top: element.scrollHeight, behavior: "auto" });
  }, [scrollRef]);

  const jumpToLatest = useCallback(() => {
    const element = scrollRef.current;
    if (!element) return;
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    following.current = true;
    autoScrolling.current = !reducedMotion;
    setAtEnd(true);
    element.scrollTo({
      top: element.scrollHeight,
      behavior: reducedMotion ? "auto" : "smooth",
    });
    window.clearTimeout(autoScrollTimer.current);
    // Fallback only: onScroll clears autoScrolling once near-end is reached.
    autoScrollTimer.current = window.setTimeout(
      () => {
        autoScrolling.current = false;
      },
      reducedMotion ? 0 : 2_000,
    );
  }, [scrollRef]);

  const scrolledJump = useRef<number | null>(null);
  const jumpScrolling = useRef(false);
  const jumpScrollTimer = useRef<number | undefined>(undefined);
  const endJumpScroll = useCallback(() => {
    jumpScrolling.current = false;
    window.clearTimeout(jumpScrollTimer.current);
    scrollRef.current?.removeEventListener("scrollend", endJumpScroll);
  }, [scrollRef]);
  // A jump scroll must win over follow-the-tail: unfollow inside the commit
  // that mounts the row so a live commit cannot cancel the animation, keep
  // retrying while the pinned window is still rendering, and suppress the
  // near-end follow re-arm only for the jump's own scroll events — scrollend
  // (or user input interrupting it, which also fires scrollend) ends the
  // suppression, with the timeout as fallback when no scroll happens.
  useLayoutEffect(() => {
    if (!scrollRequest || scrolledJump.current === scrollRequest.nonce) return;
    const element = scrollRef.current;
    const row = element?.querySelector(
      `[data-message-id="${CSS.escape(scrollRequest.messageId)}"]`,
    );
    if (!element || !row) return;
    scrolledJump.current = scrollRequest.nonce;
    following.current = false;
    autoScrolling.current = false;
    jumpScrolling.current = true;
    element.addEventListener("scrollend", endJumpScroll, { once: true });
    window.clearTimeout(jumpScrollTimer.current);
    jumpScrollTimer.current = window.setTimeout(endJumpScroll, 2_000);
    row.scrollIntoView({ behavior: "smooth", block: "center" });
    // One wash on the row so the eye lands on it (styles.css `sc-message-flash`).
    row.classList.remove("sc-message-flash");
    void (row as HTMLElement).offsetWidth;
    row.classList.add("sc-message-flash");
    row.addEventListener("animationend", () => row.classList.remove("sc-message-flash"), {
      once: true,
    });
    onScrollRequestHandled();
  }, [messages, scrollRequest, scrollRef, endJumpScroll, onScrollRequestHandled]);

  useLayoutEffect(() => {
    if (following.current) snapToEnd();
  }, [messages, running, snapToEnd]);

  // Sending always brings the conversation back to the latest message and follows the
  // reply, even if the person had scrolled up.
  useLayoutEffect(() => {
    if (!followSignal) return;
    following.current = true;
    autoScrolling.current = false;
    setAtEnd(true);
    snapToEnd();
  }, [followSignal, snapToEnd]);

  useLayoutEffect(() => {
    const button = jumpButtonRef.current;
    if (atEnd && button && document.activeElement === button) {
      button.blur();
    }
  }, [atEnd]);

  const loadOlder = useCallback(() => {
    const wasFollowing = following.current;
    // Prepend must not race the messages-driven snap-to-end follow path.
    following.current = false;
    autoScrolling.current = false;
    const pending = onLoadOlder();
    if (!pending) return;
    return Promise.resolve(pending).catch((error) => {
      const element = scrollRef.current;
      if (wasFollowing && element && transcriptIsNearEnd(element)) {
        following.current = true;
        setAtEnd(true);
      }
      throw error;
    });
  }, [onLoadOlder, scrollRef]);

  useEffect(
    () => () => {
      window.clearTimeout(autoScrollTimer.current);
      window.clearTimeout(jumpScrollTimer.current);
    },
    [],
  );

  return (
    <div className="relative flex min-h-0 flex-1">
      <div
        ref={scrollRef}
        data-testid="transcript"
        onPointerDown={(event) => {
          lastScrollTop.current = event.currentTarget.scrollTop;
          autoScrolling.current = false;
          following.current = false;
        }}
        onTouchStart={(event) => {
          lastScrollTop.current = event.currentTarget.scrollTop;
          autoScrolling.current = false;
          following.current = false;
        }}
        onWheel={(event) => {
          if (event.deltaY < 0) {
            lastScrollTop.current = event.currentTarget.scrollTop;
            autoScrolling.current = false;
            following.current = false;
          }
        }}
        onScroll={(event) => {
          const scrolledDown = transcriptMovedDown(
            lastScrollTop.current,
            event.currentTarget.scrollTop,
          );
          lastScrollTop.current = event.currentTarget.scrollTop;
          // Muse fades the transcript under the top edge once it has scrolled (styles.css).
          event.currentTarget.dataset.scrolled = String(event.currentTarget.scrollTop > 4);
          const nearEnd = transcriptIsNearEnd(event.currentTarget);
          setAtEnd(nearEnd);
          // A jump scroll owns the viewport until its animation settles; its
          // own near-end crossings must not re-arm tail-following.
          if (jumpScrolling.current) return;
          if (nearEnd) {
            if (scrolledDown) following.current = true;
            if (autoScrolling.current) {
              autoScrolling.current = false;
              window.clearTimeout(autoScrollTimer.current);
            }
          } else if (!autoScrolling.current) {
            following.current = false;
          }
        }}
        data-fade-top=""
        className={cn(
          "sc-scroll flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-4 py-5 md:px-7 md:py-6",
          "mx-auto w-full max-w-[748px] gap-[22px] overflow-x-hidden pt-8 [overflow-wrap:anywhere] md:px-6 md:pt-9 md:pb-6",
        )}
      >
        {leading}
        {onShowEarlier ? (
          <button
            type="button"
            disabled={loadingOlder}
            onClick={() => void onShowEarlier()}
            className="self-center rounded-lg px-3 py-1.5 text-[13px] text-muted-foreground hover:bg-muted hover:text-foreground/75 disabled:opacity-50"
          >
            {loadingOlder ? t`Loading…` : t`Show earlier messages`}
          </button>
        ) : null}
        {olderCursor != null ? (
          <button
            type="button"
            disabled={loadingOlder}
            onClick={() => void loadOlder()}
            className="self-center rounded-lg px-3 py-1.5 text-[13px] text-muted-foreground hover:bg-muted hover:text-foreground/75 disabled:opacity-50"
          >
            {loadingOlder ? t`Loading…` : t`Load earlier messages`}
          </button>
        ) : null}
        {loading && messages.length === 0 ? (
          <div
            role="status"
            data-testid="transcript-loading"
            className="flex flex-1 items-center justify-center text-[13px] text-muted-foreground"
          >
            {t`Loading…`}
          </div>
        ) : null}
        {transcriptRows.map((row) => {
          if (row.kind === "failures") {
            const last = row.messages.at(-1) as ThreadMessage;
            return (
              <div key={row.messages[0]?.id} data-message-id={last.id}>
                <FailureRun messages={row.messages} />
              </div>
            );
          }
          const { message } = row;
          return (
            <div
              key={message.id}
              data-message-id={message.id}
              className="group/message relative hover:z-20"
            >
              {message.id === awayMarker ? (
                <div className="pb-[22px]">
                  <AwayDivider />
                </div>
              ) : null}
              <div
                className={`relative flex ${message.role === "user" ? "justify-end" : "justify-start"}`}
              >
                <div
                  data-testid="message-bubble-frame"
                  className={`relative w-fit min-w-0 ${
                    message.role === "user" ? "max-w-[80%]" : "max-w-full"
                  }`}
                >
                  <MessageHoverActions
                    message={message}
                    side={message.role === "user" ? "start" : "end"}
                    onReply={onReply}
                    above
                  />
                  <MessageView
                    botDisplayName={botDisplayName}
                    artifactTarget={artifactTarget}
                    message={message}
                    canAnswer={hasOpenMuseAsk(message)}
                    onAnswer={onAnswer}
                    strip={strips.get(message.id) ?? null}
                    drawn={drawn}
                  />
                  {message.role === "user" && !message.id.startsWith("progress:") ? (
                    <UserMessageMeta message={message} />
                  ) : null}
                  {message.proactive ? (
                    <ProactiveMeta
                      botId={artifactTarget.botId}
                      messageId={message.id}
                      proactive={message.proactive}
                    />
                  ) : null}
                  {answeredWhileWorking.has(message.id) ? <AnsweredWhileWorking /> : null}
                </div>
              </div>
              {message.roundId ? (
                <div className="flex justify-start">
                  <RoundStrip botId={artifactTarget.botId} roundId={message.roundId} />
                </div>
              ) : null}
            </div>
          );
        })}
        {running &&
        // The newest reply's strip is lit and says what runs: no second working row.
        !lastTurnHasStrip &&
        !messages.some(
          (message) =>
            message.id.startsWith("progress:") &&
            message.blocks.some(
              (block) =>
                block.kind === "progress" && !isToolActivityBlock(block) && Boolean(block.text),
            ),
        ) ? (
          museLiveLabel ? (
            <WorkingRow label={museLiveLabel} since={workState.since} />
          ) : null
        ) : null}
        {trailing}
      </div>
      {quoteDraft && onQuote ? (
        <QuoteSelectionButton
          range={quoteDraft.range}
          onQuote={() => {
            onQuote(quoteDraft.message, quoteDraft.text);
            window.getSelection()?.removeAllRanges();
            setQuoteDraft(null);
          }}
        />
      ) : null}
      <button
        ref={jumpButtonRef}
        type="button"
        aria-label={t`Jump to latest`}
        aria-hidden={atEnd}
        tabIndex={atEnd ? -1 : 0}
        onClick={jumpToLatest}
        className={`absolute bottom-4 left-1/2 z-20 grid h-9 w-9 -translate-x-1/2 place-items-center rounded-full border border-border bg-muted/95 text-foreground/75 shadow-md backdrop-blur transition-[opacity,transform,background-color] duration-200 ease-[cubic-bezier(.22,1,.36,1)] hover:bg-border motion-reduce:transition-none ${
          atEnd ? "pointer-events-none translate-y-2 opacity-0" : "translate-y-0 opacity-100"
        }`}
      >
        <ArrowDown size={17} strokeWidth={1.8} />
      </button>
    </div>
  );
});
