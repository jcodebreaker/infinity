import { useLingui } from "@lingui/react/macro";
import type { Bot, Topic } from "@superchat/contracts";
import { AvatarStyleProvider } from "@superchat/ui-web";
import {
  Suspense,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { flushSync } from "react-dom";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  flyOrb,
  type OrbFlight,
  OrbHomeProvider,
  orbPlacement,
  SuperchatPresenceProvider,
} from "../components/ai/orb";
import { AskPreviewsProvider, FileStudioProvider } from "../components/cards/context";
import {
  transcriptWork,
  useRounds,
  useWorkState,
  WorkingPill,
  withRounds,
} from "../features/activity";
import { ApprovalCards } from "../features/approvals";
import { appsExtension } from "../features/apps";
import { ArtifactRegistryProvider } from "../features/artifacts/registry";
import { useFileStudioRoute } from "../features/artifacts/studio/useFileStudioRoute";
import { MorningPanel, useLatestBrief } from "../features/brief";
import { chartsExtension } from "../features/charts";
import { DeckThemeChoice, decksExtension } from "../features/decks";
import { documentsExtension } from "../features/documents";
import { FolderPicker } from "../features/files/FolderPicker";
import { knowledgeExtension } from "../features/knowledge";
import { liveExtension } from "../features/live";
import { ConversationModelPicker } from "../features/models";
import { reportRhythm, setOutboxUnread, useUnreadTitle } from "../features/outbox";
import { useChatPlace, WorkInChip } from "../features/projects/WorkIn";
import { sheetsExtension } from "../features/sheets";
import {
  FLIGHT_HANDOFF_MS,
  flyCard,
  HomeTopics,
  isFresh,
  TopicsPanel,
  TopicTag,
  tagFor,
  topicHues,
  topicPalette,
  useTopics,
} from "../features/topics";
import type { ArtifactTarget } from "../lib/artifact-open";
import { markAfterPaint, markOnce } from "../lib/performance";
import { rpc } from "../lib/rpc";
import { ShowMessageProvider } from "../lib/show-message";
import { useTurnRuns } from "../lib/useMuseLiveState";
import { LogoHomeButton, PullHomeTab } from "./app/chrome/BackHome";
import { ContinuePill } from "./app/chrome/ContinuePill";
import { ContourGround } from "./app/chrome/ContourGround";
import { ConversationActivity } from "./app/chrome/ConversationActivity";
import { conversationLayout } from "./app/chrome/conversationLayout";
import type { DockKey } from "./app/chrome/Dock";
import { ConnectedDock, SettingsButton } from "./app/chrome/Dock";
import {
  EmptyConversationLead,
  EmptyConversationSuggestions,
} from "./app/chrome/EmptyConversation";
import { Home } from "./app/chrome/Home";
import {
  lastLine,
  type SlideWay,
  slideViews,
  usePullPastStart,
  useSlideKeys,
} from "./app/chrome/homeSlide";
import { BotSettingsPanel } from "./app/chrome/PanelForms";
import type { Panel } from "./app/chrome/panel";
import { SidePanelHeader } from "./app/chrome/SidePanelHeader";
import { useBotRoster } from "./app/chrome/useBotRoster";
import { useBrowserNotifications } from "./app/chrome/useBrowserNotifications";
import { useReplyAlerts } from "./app/chrome/useReplyAlerts";
import { ClearConversationHost } from "./app/conversation/ClearConversationHost";
import { Composer } from "./app/conversation/Composer";
import { Transcript } from "./app/conversation/Transcript";
import { useComposerSend } from "./app/conversation/useComposerSend";
import { useDockTransition } from "./app/conversation/useDockTransition";
import { useMuseTranscript } from "./app/conversation/useMuseTranscript";
import { useTranscriptScroll } from "./app/conversation/useTranscriptScroll";
import { SettingsHost } from "./app/settings/SettingsHost";
import { useShellSettings } from "./app/settings/useShellSettings";
import { DockSheets } from "./app/sheets/DockSheets";

/** Superchat's Mac window (the old design notes "Window"): the Conversation and the other screens
 * sit flat on the rounded content window; only the sidebar, the inspector and side panels
 * float as glass inside it. */
const MUSE_CONTENT_PANE = "relative flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden";

/** What the capabilities built on artifacts add to the panel and the Library cards. */
const ARTIFACT_EXTENSIONS = [
  appsExtension,
  sheetsExtension,
  decksExtension,
  documentsExtension,
  chartsExtension,
  knowledgeExtension,
  liveExtension,
];

/** Ask cards whose options preview a capability's things, drawn by that capability. */
const ASK_PREVIEWS = { "deck-theme": DeckThemeChoice };

export function ShellPage() {
  const { t } = useLingui();
  const { botId } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  const [panel, setPanel] = useState<Panel>(null);
  const { bots, botsRef, initialBotsLoaded, bootstrapMe } = useBotRoster({
    botId,
    navigate,
  });
  const userId = bootstrapMe?.userId;

  const settings = useShellSettings();
  const { openSettings } = settings;

  const [clearTarget, setClearTarget] = useState<Bot | null>(null);

  const active = bots.find((b) => b.id === botId) ?? bots[0];
  // The dock place open as a sheet, if any.
  const [dockSheet, setDockSheet] = useState<DockKey | null>(null);
  // Home is the app's first screen (ADR 0035, amended): every load opens on it, over the
  // Conversation. Continuing slides the Conversation in (`slide` "up"); the Conversation's logo,
  // a pull past its top or ⌘↑ slides Home back ("down"). A send from Home and a message link
  // (`?m=`) land in the Conversation at once; a picked-up topic flies in and slides.
  const [homeOpen, setHomeOpen] = useState(true);
  const [slide, setSlide] = useState<SlideWay | null>(null);
  const [topicsOpen, setTopicsOpen] = useState(false);
  const { topics, reload: reloadTopics } = useTopics(active?.id);
  // The topics' colours, built as Home's row builds them, so the morning panel's tags match.
  const topicColours = useMemo(
    () => topicPalette((topics ?? []).map((topic) => topic.key)),
    [topics],
  );
  // The morning brief leads Home's topics only when there is one (ADR 0037).
  const latestBrief = useLatestBrief(active?.id ?? "");
  const [folderPickerOpen, setFolderPickerOpen] = useState(false);
  /** Below `md` the Muse sidebar is an off-canvas drawer opened from the main header. */
  const [_navOpen, _setNavOpen] = useState(false);
  const activeBotId = useRef<string | undefined>(active?.id);
  activeBotId.current = active?.id;
  const { flushPendingBrowserNotifications, notifyBrowserForEvent } =
    useBrowserNotifications(botsRef);
  const { messageScroll, scrollRequest, clearScrollRequest, scrollToMessage } =
    useTranscriptScroll();

  useEffect(() => {
    const messageId = searchParams.get("m");
    if (!active) return;
    if (messageId) {
      // The Muse's Conversation is the engine's transcript: page back to the hit, then scroll.
      void revealMessageRef
        .current(messageId)
        .then((found) => {
          if (found) scrollToMessage(messageId);
        })
        .finally(() => {
          setHomeOpen(false);
          const next = new URLSearchParams(searchParams);
          next.delete("m");
          setSearchParams(next, { replace: true });
        });
    }
  }, [active?.id, searchParams, setSearchParams]);
  const revealMessageRef = useRef<(messageId: string) => Promise<boolean>>(async () => false);
  // A version's "from your message": the same reveal-and-scroll a search hit uses (`?m=`).
  const showMessage = useCallback(
    (itemId: string) =>
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current);
          next.set("m", itemId);
          return next;
        },
        { replace: true },
      ),
    [setSearchParams],
  );
  // The File Studio (ADR 0030): a route state over the app; closing it comes back here.
  const fileStudio = useFileStudioRoute(showMessage);
  // Bumps each time a turn finishes, so the Files view follows the Muse's work.
  const [filesRefreshKey, setFilesRefreshKey] = useState(0);
  // The folder to show in the Files tab, set by the "Working in" chip.
  const [filesReveal, setFilesReveal] = useState<{ path: string; nonce: number } | null>(null);
  const showFolderInFiles = useCallback((path: string) => {
    setFilesReveal((current) => ({ path, nonce: (current?.nonce ?? 0) + 1 }));
    setDockSheet("files");
  }, []);
  const { place: conversationPlace, reload: reloadConversationPlace } = useChatPlace(
    active ? () => rpc.chats.project({ botId: active.id }) : undefined,
    active?.id ?? "",
    filesRefreshKey,
  );
  const listWorkspaceFolder = useCallback(
    (path: string) =>
      active
        ? rpc.files.list({ botId: active.id, path }).then(({ entries }) => entries)
        : Promise.resolve([]),
    [active?.id],
  );
  // The Muse's Conversation is the engine's transcript (ADR 0009), and so is whether it is working.
  const museTranscript = useMuseTranscript(active?.id, messageScroll);
  // "(2) superchat" while messages Superchat wrote on its own wait unseen (ADR 0036).
  useUnreadTitle(active?.id);
  useReplyAlerts({
    bot: active,
    conversationId: museTranscript.threadId,
    notifyBrowserForEvent,
  });
  const readTranscript = museTranscript.refresh;
  revealMessageRef.current = museTranscript.reveal;
  // The person's message on its way, then the reply as it streams, until the transcript has them.
  const { messages: storedMessages, pendingMessage, liveMessage } = museTranscript;
  // The Conversation's Rounds (ADR 0029): the message that opened one carries its id.
  const rounds = useRounds(active?.id);
  // Only a new Round changes the transcript; its updates are its strip's.
  const roundKey = rounds.map((round) => round.roundId).join(" ");
  const transcriptMessages = useMemo(() => {
    const unstored = [pendingMessage, liveMessage].filter((message) => message !== null);
    const stored = withRounds(storedMessages ?? [], new Set(roundKey.split(" ")));
    return unstored.length ? [...stored, ...unstored] : stored;
  }, [storedMessages, pendingMessage, liveMessage, roundKey]);
  // Each reply's strip from before Rounds and where the running work began, read once.
  const work = useMemo(() => transcriptWork(transcriptMessages), [transcriptMessages]);
  const {
    activePendingAttachments,
    activePendingFolders,
    attachFolder,
    removeFolder,
    setReplyTarget,
    setReplyQuote,
    replyTarget,
    replyQuote,
    clearReply,
    sending,
    stopping,
    stopFailed,
    clearStopFailed,
    sendError,
    composerSeed,
    setComposerSeed,
    attachmentNotice,
    uploadStatus,
    fileInputRef,
    followSignal,
    failure,
    dismissComposerError,
    answerMessage,
    onAttachmentPick,
    removeAttachment,
    retryAttachment,
    sendMessage,
    sendCardReply,
    stopRun,
  } = useComposerSend({
    active,
    activeBotId,
    userId,
    messages: transcriptMessages,
    transcript: museTranscript,
    botsRef,
    flushPendingBrowserNotifications,
  });
  // Whether Superchat is working: the one reading every surface below takes as a prop.
  const workState = useWorkState({
    botId: active?.id,
    turn: museTranscript.turn,
    sending,
    stopping,
    turnStartedAt: work.runningSince ?? null,
    rounds,
    roundId: transcriptMessages.findLast((message) => message.role === "user")?.roundId,
  });
  const running = workState.running;
  const turnRuns = useTurnRuns(`conversation:${active?.id}`, workState.turnRunning);
  // "Couldn't stop" says so only while the work still runs.
  useEffect(() => {
    if (!running) clearStopFailed();
  }, [running, clearStopFailed]);
  const wasRunningRef = useRef(false);
  useEffect(() => {
    if (wasRunningRef.current && !running) {
      setFilesRefreshKey((key) => key + 1);
      // A turn on a picked-up topic updates it before it ends (ADR 0035, amended).
      void reloadTopics();
    }
    wasRunningRef.current = running;
  }, [running, reloadTopics]);
  // An empty Conversation is a start page: the greeting, the composer centered under it, and
  // a few suggestions; the composer glides down to its dock once the first message lands.
  const startPage =
    conversationLayout({
      hasMuse: Boolean(active),
      loaded: museTranscript.messages !== null,
      messageCount: transcriptMessages.length,
      running,
    }) === "start";
  // The start page stays on screen until the orb's flight begins: `shownStart` follows
  // `startPage`, but leaving it runs through flyOrb (a View Transition, a FLIP, or a cut under
  // reduced motion), so the orb reads as one object moving from the center to its home.
  const [shownStart, setShownStart] = useState(startPage);
  const orbFlight = useRef<OrbFlight | null>(null);
  const flightQueued = useRef(false);
  const latestStartPage = useRef(startPage);
  latestStartPage.current = startPage;
  useLayoutEffect(() => {
    if (startPage === shownStart) return;
    if (startPage) {
      setShownStart(true);
      return;
    }
    if (flightQueued.current) return;
    flightQueued.current = true;
    // Out of the commit phase, so the update can flush synchronously inside the transition.
    queueMicrotask(() => {
      flightQueued.current = false;
      if (latestStartPage.current) return;
      orbFlight.current = flyOrb(() => flushSync(() => setShownStart(false)));
    });
  }, [startPage, shownStart]);
  // The person's message on its way opens the Conversation.
  useEffect(() => {
    if (pendingMessage) setHomeOpen(false);
  }, [pendingMessage]);
  // Home is also the start page of an empty Conversation.
  const showHome = Boolean(active && (shownStart || homeOpen));
  const shellRoot = useRef<HTMLDivElement>(null);
  const composerDock = useRef<HTMLDivElement>(null);
  const homeLayer = useRef<HTMLDivElement>(null);
  const chatLayer = useRef<HTMLDivElement>(null);
  // While one side slides away it keeps its composer's room, so nothing shifts as it goes.
  const heldComposer = useRef(0);
  const homeOpenRef = useRef(homeOpen);
  homeOpenRef.current = homeOpen;
  const slideTo = useCallback((way: SlideWay) => {
    if (homeOpenRef.current === (way === "down")) return;
    const from = way === "up" ? homeLayer.current : composerDock.current;
    const room = way === "up" ? from?.querySelector("[data-home-composer]") : from;
    heldComposer.current = room?.getBoundingClientRect().height ?? 0;
    setTopicsOpen(false);
    if (way === "up") setDockSheet(null);
    setHomeOpen(way === "down");
    setSlide(way);
  }, []);
  useLayoutEffect(() => {
    if (!slide) return;
    const toEnd = () => {
      const scroller = messageScroll.current;
      if (scroller) scroller.scrollTop = scroller.scrollHeight;
    };
    if (slide === "up") toEnd();
    const run = slideViews(homeLayer.current, chatLayer.current, slide);
    let live = true;
    void run.done.then(() => {
      if (!live) return;
      if (slide === "up") toEnd();
      // Into the composer of the side that came in.
      const into = slide === "up" ? composerDock.current : homeLayer.current;
      into?.querySelector<HTMLElement>("textarea")?.focus({ preventScroll: true });
      setSlide(null);
    });
    return () => {
      live = false;
      run.cancel();
    };
  }, [slide, messageScroll]);
  // "Continue the conversation": the Conversation's last line, while it has one.
  const last = useMemo(() => lastLine(transcriptMessages), [transcriptMessages]);
  const continueChat = useCallback(() => slideTo("up"), [slideTo]);
  const backHome = useCallback(() => slideTo("down"), [slideTo]);
  // The Conversation in front, with nothing over it: where the way back to Home applies.
  const chatInFront = Boolean(active) && !showHome && slide === null && !fileStudio.open;
  // Pulling past the top of the Conversation goes back to Home; never while an earlier page
  // loads, and only after a held top while earlier messages can still be loaded.
  const pullHome = usePullPastStart(
    chatInFront && !museTranscript.loadingOlder ? backHome : null,
    museTranscript.olderCursor != null || museTranscript.canShowEarlier ? 400 : 0,
  );
  // ⌘↑ back to Home from the Conversation; ⌘↓ from Home continues, like the pill.
  useSlideKeys(
    chatInFront ? backHome : null,
    showHome && slide === null && last && !shownStart && !fileStudio.open ? continueChat : null,
  );
  // The Conversation's own chrome (its ground, logo and dock) while it shows or slides.
  const chatChrome = active && (!showHome || slide !== null) ? active : null;
  const homeComposerFocus = useCallback(() => {
    homeLayer.current?.querySelector<HTMLElement>("textarea")?.focus();
  }, []);
  // Home shown again: what the Conversation just did on a topic, and a new brief, show at once.
  const reloadBrief = latestBrief.reload;
  useEffect(() => {
    if (showHome || topicsOpen) void reloadTopics();
    if (showHome) void reloadBrief();
  }, [showHome, topicsOpen, reloadTopics, reloadBrief]);
  // Picking a topic up adds its card to the Conversation (nothing is sent) and opens it there.
  // From Home the card flies into the thread while the pick is recorded.
  const pickTopic = useCallback(
    async (topic: Topic, from?: HTMLElement) => {
      if (!active) return;
      const into = shellRoot.current?.getBoundingClientRect();
      const flight = from && into ? flyCard(from, into) : null;
      try {
        await Promise.all([
          rpc.topics.pick({ botId: active.id, key: topic.key }),
          flight ? new Promise((done) => window.setTimeout(done, FLIGHT_HANDOFF_MS)) : null,
        ]);
      } catch {
        flight?.cancel();
        return;
      }
      setTopicsOpen(false);
      readTranscript();
      if (homeOpenRef.current) slideTo("up");
    },
    [active?.id, readTranscript, slideTo],
  );
  // "Review the draft" on Home: open the file the Lookout prepared, count its message as seen
  // and acted on (ADR 0038), so the topic stops leading Home once the person opened it.
  const reviewDraft = useCallback(
    (topic: Topic) => {
      const prepared = topic.prepared;
      if (!active || !prepared?.artifactId) return;
      fileStudio.api.open(prepared.artifactId, prepared.title || topic.title);
      reportRhythm(active.id, prepared.itemId, "acted_on");
      rpc.outbox
        .seen({ botId: active.id, itemId: prepared.itemId })
        .then((next) => setOutboxUnread(active.id, next))
        .catch(() => undefined)
        .finally(() => void reloadTopics());
    },
    [active?.id, fileStudio.api, reloadTopics],
  );
  const dockTopics = useMemo(
    () => ({
      open: topicsOpen,
      fresh: Boolean(topics?.some((topic) => isFresh(topic, new Date()))),
      onToggle: () => setTopicsOpen((open) => !open),
    }),
    [topicsOpen, topics],
  );
  // A View Transition already glides the composer (`superchat-composer`); the FLIP is the fallback.
  useDockTransition(composerDock, shownStart, () => orbFlight.current === "view-transition");
  const orbHome = orbPlacement({
    startPage: shownStart,
    sidebarVisible: false,
  });
  const transcriptArtifactTarget = useMemo<ArtifactTarget>(
    () => ({ botId: active?.id ?? "" }),
    [active?.id],
  );
  const shellReady =
    initialBotsLoaded && (bots.length === 0 || Boolean(active && museTranscript.messages));

  useLayoutEffect(() => {
    if (initialBotsLoaded) {
      markOnce("rk:renderer:bots-committed");
      markAfterPaint("rk:renderer:bots-painted");
    }
    if (active && museTranscript.messages) {
      markOnce("rk:renderer:thread-committed");
      markAfterPaint("rk:renderer:thread-painted");
    }
    if (shellReady) {
      markOnce("rk:renderer:shell-ready");
      markAfterPaint("rk:renderer:shell-painted");
    }
  }, [active, initialBotsLoaded, shellReady, museTranscript.messages]);

  const replyTargetName = replyTarget
    ? replyTarget.role === "user"
      ? t`You`
      : (active?.name ?? t`Bot`)
    : undefined;
  const startChat = useCallback(
    (input: { send?: string; seed?: string }) => {
      if (input.seed) setComposerSeed(input.seed);
      if (input.send) void sendMessage(input.send);
    },
    [sendMessage],
  );
  const userName = bootstrapMe?.name ?? t`You`;

  const composerEl = (
    <Composer
      key={`bot:${active?.id}`}
      activeName={active?.name}
      phase={workState.phase}
      stopFailed={stopFailed && running}
      pendingAttachments={activePendingAttachments}
      pendingFolders={activePendingFolders}
      onAttachFolder={active ? () => setFolderPickerOpen(true) : undefined}
      onRemoveFolder={removeFolder}
      attachmentNotice={attachmentNotice}
      uploadStatus={uploadStatus}
      sendError={sendError}
      failure={failure}
      onDismissError={dismissComposerError}
      fileInputRef={fileInputRef}
      onAttachmentPick={onAttachmentPick}
      onRemoveAttachment={removeAttachment}
      onRetryAttachment={retryAttachment}
      onSend={(...args: Parameters<typeof sendMessage>) => {
        return sendMessage(...args);
      }}
      seedText={composerSeed}
      onSeedConsumed={() => setComposerSeed(null)}
      onStop={stopRun}
      replyTarget={replyTarget}
      replyQuote={replyQuote}
      replyTargetName={replyTargetName}
      onClearReply={clearReply}
      onSlashAction={(action) => {
        if (action === "chat-settings") {
          setPanel("settings");
          return;
        }
        if (action === "settings-general") {
          openSettings("general");
          return;
        }
      }}
    />
  );

  // The superchat top bar: the Project and the model.
  const superchatTopBar = active ? (
    <div className="app-drag relative z-10 flex h-[84px] min-w-0 shrink-0 items-center gap-3 ps-4 pe-4 md:ps-0 md:pe-6">
      <LogoHomeButton onHome={backHome} className="md:hidden" />
      <span className="min-w-0 flex-1" />
      <div className="app-no-drag flex shrink-0 items-center gap-2">
        {/* Running work shows here too, for when its strip is scrolled away (ADR 0023). */}
        <WorkingPill work={workState} botId={active?.id} />
        <span className="app-no-drag hidden md:inline-flex">
          <WorkInChip
            align="end"
            place={conversationPlace}
            list={listWorkspaceFolder}
            onShow={showFolderInFiles}
            onPick={(folder) =>
              rpc.chats.setFolder({ botId: active.id, folder }).then(reloadConversationPlace)
            }
          />
        </span>
        <ConversationModelPicker botId={active.id} />
      </div>
    </div>
  ) : null;

  const shell = (
    <div
      ref={shellRoot}
      data-testid="shell-root"
      data-ready={shellReady}
      className="superchat-window relative flex h-full min-w-0 flex-1 overflow-hidden text-foreground"
    >
      {/* Super Chat stays where it was under an open file, out of reach of the keyboard. */}
      <div className="contents" inert={fileStudio.open || undefined}>
        {(showHome || slide === "up") && active ? (
          <Home
            ref={homeLayer}
            away={!showHome}
            personName={bootstrapMe?.name ?? ""}
            dock={
              <ConnectedDock
                botId={active.id}
                active={dockSheet}
                onSelect={setDockSheet}
                topics={dockTopics}
                conversationShown={false}
                onConversation={() => {
                  setDockSheet(null);
                  if (last && !shownStart) continueChat();
                  else homeComposerFocus();
                }}
              />
            }
            composer={
              showHome ? (
                composerEl
              ) : (
                <div aria-hidden="true" style={{ height: heldComposer.current }} />
              )
            }
            topics={
              <HomeTopics
                topics={topics}
                onPick={(topic, from) => void pickTopic(topic, from)}
                onReviewDraft={reviewDraft}
                after={
                  latestBrief.brief?.page ? (
                    <MorningPanel
                      latest={latestBrief}
                      tag={(line) =>
                        line.topicKey ? (
                          <TopicTag
                            small
                            hues={topicHues(line.topicKey, topicColours)}
                            label={tagFor(topics, line.topicKey, line.topic)}
                          />
                        ) : null
                      }
                    />
                  ) : undefined
                }
              />
            }
            onOpenSettings={() => openSettings("general")}
            recent={<ConversationActivity botId={active.id} />}
            onContinue={continueChat}
            continuePill={
              last && !shownStart ? (
                <ContinuePill line={last.text} at={last.at} onContinue={continueChat} />
              ) : undefined
            }
          />
        ) : null}
        {/* The Conversation: under Home (out of reach) until it slides in. */}
        <div
          ref={chatLayer}
          data-testid="conversation-layer"
          inert={showHome || undefined}
          className={active ? "relative flex min-w-0 flex-1" : "contents"}
        >
          {chatChrome ? <ContourGround /> : null}
          {chatChrome ? (
            <div className="absolute start-8 top-7 z-20 hidden items-center md:flex">
              <LogoHomeButton onHome={backHome} />
            </div>
          ) : null}
          {chatChrome ? (
            <div className="relative z-10 hidden w-[96px] shrink-0 flex-col items-center justify-between pt-24 pb-6 md:flex">
              <span />
              <ConnectedDock
                botId={chatChrome.id}
                active={dockSheet}
                onSelect={setDockSheet}
                topics={dockTopics}
                conversationShown={!showHome}
                onConversation={() => setDockSheet(null)}
              />
              <SettingsButton onClick={() => openSettings("general")} />
            </div>
          ) : null}

          <main className="relative z-10 flex min-w-0 flex-1 flex-col">
            <div className={active ? "flex min-h-0 flex-1" : "contents"}>
              <div className={active ? MUSE_CONTENT_PANE : "contents"}>
                {superchatTopBar}
                {shownStart ? (
                  <EmptyConversationLead personName={bootstrapMe?.name ?? ""} />
                ) : active ? (
                  <div className="relative flex min-h-0 flex-1 flex-col" {...pullHome.handlers}>
                    <PullHomeTab
                      pull={pullHome.pull}
                      topicCount={topics?.length ?? 0}
                      onHome={backHome}
                    />
                    <Transcript
                      key={museTranscript.threadId ?? active.id}
                      botDisplayName={active.name}
                      followSignal={followSignal}
                      museFace={{ color: active.color, identity: active.id }}
                      museRuns={turnRuns}
                      scrollRef={messageScroll}
                      scrollRequest={scrollRequest}
                      onScrollRequestHandled={clearScrollRequest}
                      artifactTarget={transcriptArtifactTarget}
                      messages={transcriptMessages}
                      loading={museTranscript.messages === null}
                      olderCursor={museTranscript.olderCursor}
                      loadingOlder={museTranscript.loadingOlder}
                      work={work}
                      workState={workState}
                      onLoadOlder={museTranscript.loadOlder}
                      onShowEarlier={
                        museTranscript.canShowEarlier ? museTranscript.showEarlier : undefined
                      }
                      onAnswer={answerMessage}
                      onSendCard={sendCardReply}
                      onDraftCard={setComposerSeed}
                      onReply={(message) => {
                        setReplyTarget(message);
                        setReplyQuote(null);
                      }}
                      onQuote={(message, quote) => {
                        setReplyTarget(message);
                        setReplyQuote(quote);
                      }}
                      trailing={<ApprovalCards botId={active.id} />}
                      shown={
                        dockSheet === null &&
                        !fileStudio.open &&
                        !showHome &&
                        slide === null &&
                        !topicsOpen
                      }
                    />
                  </div>
                ) : null}
                {active ? (
                  <div
                    ref={composerDock}
                    style={{
                      viewTransitionName: "superchat-composer",
                      minHeight: slide === "down" ? heldComposer.current : undefined,
                    }}
                  >
                    {showHome ? null : composerEl}
                  </div>
                ) : null}
                {shownStart ? (
                  <EmptyConversationSuggestions onSend={(text) => void sendMessage(text)} />
                ) : null}
              </div>
              {active ? (
                <div className="pointer-events-none absolute end-5 bottom-24 z-20 hidden md:block xl:bottom-6 [&>*]:pointer-events-auto">
                  <ConversationActivity botId={active.id} />
                </div>
              ) : null}
            </div>
          </main>
        </div>

        {active ? (
          <TopicsPanel
            open={topicsOpen}
            topics={topics}
            onClose={() => setTopicsOpen(false)}
            onPick={(topic) => void pickTopic(topic)}
          />
        ) : null}
        {active ? (
          <FolderPicker
            botId={active.id}
            open={folderPickerOpen}
            onClose={() => setFolderPickerOpen(false)}
            onPick={attachFolder}
          />
        ) : null}
        {active ? (
          <DockSheets
            botId={active.id}
            sheet={dockSheet}
            onClose={() => setDockSheet(null)}
            filesRefreshKey={filesRefreshKey}
            filesReveal={filesReveal}
            onStartChat={startChat}
          />
        ) : null}

        <aside
          data-testid="side-panel"
          data-panel={panel ?? "closed"}
          className={`superchat-glass absolute inset-y-0 end-0 z-20 flex min-h-0 shrink-0 flex-col overflow-hidden transition-[width] duration-150 ease-out md:relative ${
            panel && active
              ? // An inset panel like <main>.
                "w-full max-w-[400px] md:m-2 md:w-[400px] md:max-w-none"
              : "pointer-events-none w-0"
          }`}
        >
          {panel && active ? (
            <div className="sc-scroll h-full w-full overflow-y-auto px-5 py-[17px] md:w-[400px]">
              <SidePanelHeader panel={panel} setPanel={setPanel} active={active} />
              {panel === "settings" && active ? (
                <BotSettingsPanel onClear={() => setClearTarget(active)} />
              ) : null}
            </div>
          ) : null}
        </aside>
      </div>
      {active ? fileStudio.studio : null}

      <Suspense fallback={null}>
        {clearTarget ? (
          <ClearConversationHost
            bot={clearTarget}
            onClose={() => setClearTarget(null)}
            onCleared={readTranscript}
          />
        ) : null}
      </Suspense>

      <Suspense fallback={null}>
        <SettingsHost
          settings={settings}
          userName={userName}
          email={bootstrapMe?.userId}
          active={active}
        />
      </Suspense>
    </div>
  );

  return (
    <AvatarStyleProvider value={bootstrapMe?.avatarStyle ?? "robot"}>
      <FileStudioProvider value={fileStudio.api}>
        <ShowMessageProvider value={showMessage}>
          <ArtifactRegistryProvider value={ARTIFACT_EXTENSIONS}>
            <AskPreviewsProvider value={ASK_PREVIEWS}>
              {/* The window ground around Superchat's rounded content window. */}
              <div className="muse-wash pt-safe pb-safe ps-safe pe-safe flex h-full md:p-2">
                {active ? (
                  <OrbHomeProvider home={orbHome}>
                    {/* Every orb brightens while Superchat works. */}
                    <SuperchatPresenceProvider state={running ? "working" : "idle"}>
                      {shell}
                    </SuperchatPresenceProvider>
                  </OrbHomeProvider>
                ) : (
                  shell
                )}
              </div>
            </AskPreviewsProvider>
          </ArtifactRegistryProvider>
        </ShowMessageProvider>
      </FileStudioProvider>
    </AvatarStyleProvider>
  );
}
