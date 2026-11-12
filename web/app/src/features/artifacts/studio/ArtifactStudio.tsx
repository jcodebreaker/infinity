import { plural } from "@lingui/core/macro";
import { useLingui } from "@lingui/react/macro";
import { ChatMarkdown } from "@superchat/chat-ui/web";
import type { ArtifactVersion } from "@superchat/contracts";
import { Button, cn } from "@superchat/ui-web";
import { ChevronLeft, CornerDownRight, History, Pencil } from "lucide-react";
import type { ReactNode } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { SuperchatOrb } from "../../../components/ai/orb";
import { useNow } from "../../../components/chrome/useNow";
import { ShowMessageProvider, useShowMessage } from "../../../lib/show-message";
import { formatClock, serverNow, useActivities } from "../../activity";
import { formatDayAndTime } from "../../activity/activityGrouping";
import type { ArtifactContent } from "../editor/ArtifactEditor";
import { ArtifactEditor, isTypingTarget } from "../editor/ArtifactEditor";
import { splitElementAsk } from "../editor/html-ask";
import { useArtifactContent } from "../library/useArtifactContent";
import { useArtifactPanelParts } from "../registry";
import type { StudioRow } from "./thread";
import { studioThread, versionOrigin } from "./thread";
import { useOriginLabel } from "./useOriginLabel";
import { usePhoneLayout } from "./usePhoneLayout";
import type { StudioChat } from "./useStudioChat";
import { useStudioChat } from "./useStudioChat";

/** How long a message picked from the version menu stays marked. */
const HIGHLIGHT_MS = 2200;

type StudioProps = {
  title?: string;
  /** Open a PDF at this page (a citation). */
  page?: number;
  /** Back to Super Chat; with an item id, scrolled to that message ("Show in Super Chat"). */
  onClose: (revealItemId?: string) => void;
};

/**
 * The File Studio (ADR 0030): one view for a file, opened from its card, the Library or a
 * citation. The file's conversation on the left (its prelude from Super Chat, the studio thread,
 * the turn's live strip, the composer with the selection chip), the file on the right in the one
 * editor (title bar, slide rail, canvas). Requests go to the file's studio session, never to the
 * Conversation. "‹ Super Chat", close, and Escape with nothing selected go back.
 */
export function ArtifactStudio({
  artifactId,
  onResolved,
  ...props
}: StudioProps & {
  /** Any version of the file, or its group id (the route's `?file=`). */
  artifactId: string;
  /** The file's group id once known, for the route to name the file by it. */
  onResolved?: (groupId: string) => void;
}) {
  // The route moving from a version id to the file's group id is the same file: no reload.
  const [familyId, setFamilyId] = useState(artifactId);
  const content = useArtifactContent(familyId);
  const ready = content.status === "ready" ? content : null;
  const groupId = ready?.artifact.groupId ?? null;
  if (
    artifactId !== familyId &&
    artifactId !== groupId &&
    !content.versions.some((version) => version.id === artifactId)
  ) {
    setFamilyId(artifactId);
  }
  useEffect(() => {
    if (groupId && groupId !== artifactId) onResolved?.(groupId);
  }, [groupId, artifactId, onResolved]);
  const chat = useStudioChat({
    artifactId: familyId,
    botId: ready?.artifact.botId ?? null,
    groupId,
    onSaved: content.refresh,
  });
  return <StudioView content={content} chat={chat} {...props} />;
}

/** The studio from its file and its chat (the dev page hands it fixtures). */
export function StudioView({
  content,
  chat,
  title,
  page,
  onClose,
}: StudioProps & { content: ArtifactContent; chat: StudioChat }) {
  const { t } = useLingui();
  const ready = content.status === "ready" ? content : null;
  const panel = useArtifactPanelParts({
    artifact: ready?.artifact,
    title,
    bytes: ready?.bytes,
    newestVersion: content.versions[0] ?? null,
  });
  const phone = usePhoneLayout();
  // On a phone the conversation is a sheet under the file: a peek row until raised.
  const [raised, setRaised] = useState(false);
  // How to end the canvas selection while there is one: Escape does that before it leaves.
  const clearSelection = useRef<(() => void) | null>(null);
  const onSelection = useCallback(
    (clear: (() => void) | null) => {
      clearSelection.current = clear;
      if (clear && phone) setRaised(true); // selecting raises the sheet with the chip in it
    },
    [phone],
  );

  // Escape: a menu, a dialog or an overlay of the file's own (the Theme gallery) takes it first;
  // then the selection; then, on a phone, the raised sheet; then the studio closes.
  const holdsEscape = panel.holdsEscape;
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented || holdsEscape) return;
      const target = event.target instanceof Element ? event.target : null;
      if (isTypingTarget(target) || target?.closest('[role="menu"],[role="dialog"]')) return;
      if (clearSelection.current) clearSelection.current();
      else if (phone && raised) setRaised(false);
      else onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [holdsEscape, phone, raised, onClose]);

  // The studio takes focus when it opens, so the keyboard is in it, not behind it.
  const root = useRef<HTMLElement>(null);
  useEffect(() => root.current?.focus({ preventScroll: true }), []);

  // "Show in Super Chat" (the prelude, the version menu) leaves the studio at that message.
  const showMessage = useCallback((itemId: string) => onClose(itemId), [onClose]);

  const originLabel = useOriginLabel();
  const versionLabel = (version: ArtifactVersion) =>
    originLabel(versionOrigin(version, chat.sessionId));

  const rows = useMemo(
    () => studioThread(chat.messages, content.versions, chat.sessionId, chat.prelude),
    [chat.messages, content.versions, chat.sessionId, chat.prelude],
  );
  // The thread paints once, whole: its transcript, its prelude and the versions together.
  const threadReady = chat.loaded && (ready !== null || content.status === "error");

  // A version of this chat picked from the menu marks the message that made it.
  const [highlight, setHighlight] = useState<string | null>(null);
  const shown = content.versions.find((version) => version.id === content.versionId);
  const pickedOlder = !!shown && shown.id !== content.versions[0]?.id;
  const picked = pickedOlder && shown?.fromStudio ? (shown.itemId ?? null) : null;
  useEffect(() => {
    if (!picked) return;
    setHighlight(picked);
    const timer = window.setTimeout(() => setHighlight(null), HIGHLIGHT_MS);
    return () => window.clearTimeout(timer);
  }, [picked]);

  const versionCount = content.versions.length;
  const aside = (askBox: ReactNode) => (
    <aside
      data-testid="studio-chat"
      data-raised={phone ? raised : undefined}
      aria-label={t`This file's conversation`}
      onFocus={phone ? () => setRaised(true) : undefined}
      className={cn(
        "flex min-h-0 flex-col bg-background",
        phone
          ? cn(
              "absolute inset-x-0 bottom-0 z-30 rounded-t-[18px] border-t border-border bg-content shadow-float motion-safe:transition-[height] motion-safe:duration-300 motion-safe:ease-out",
              raised ? "h-[82%]" : "h-[276px]",
            )
          : "w-[384px] shrink-0 border-e border-border",
      )}
    >
      {phone ? (
        <button
          type="button"
          aria-label={raised ? t`Lower the conversation` : t`Raise the conversation`}
          aria-expanded={raised}
          data-testid="studio-chat-grab"
          onClick={() => setRaised((on) => !on)}
          className="mx-auto mt-1.5 h-4 w-12 shrink-0 rounded-full after:mx-auto after:block after:h-[5px] after:w-9 after:rounded-full after:bg-separator"
        />
      ) : null}
      <header className="flex shrink-0 items-center gap-1.5 ps-2.5 pe-3.5 pt-3 pb-2">
        <Button
          variant="ghost"
          size="sm"
          aria-label={t`Back to Super Chat`}
          data-testid="studio-back"
          className="text-muted-foreground"
          onClick={() => onClose()}
        >
          <ChevronLeft />
          {t`Super Chat`}
        </Button>
        {versionCount > 0 ? (
          <span data-testid="studio-chat-summary" className="ms-auto text-[12px] text-ink-3">
            {plural(versionCount, { one: "# version", other: "# versions" })}
          </span>
        ) : null}
      </header>
      <StudioThread
        rows={rows}
        ready={threadReady}
        failed={chat.failed}
        peek={phone && !raised}
        highlight={highlight}
        live={
          chat.running && ready?.artifact.botId && chat.sessionId ? (
            <TurnStrip
              botId={ready.artifact.botId}
              sessionId={chat.sessionId}
              about={latestChip(rows)}
              waiting={chat.blockedOn}
            />
          ) : null
        }
      />
      <div className="shrink-0 px-3 pt-1 pb-3">{askBox}</div>
    </aside>
  );

  return (
    <section
      ref={root}
      tabIndex={-1}
      data-testid="artifact-studio"
      data-layout={phone ? "phone" : "wide"}
      aria-label={title || ready?.artifact.name || t`File`}
      className={cn(
        "relative flex h-full min-h-0 w-full overflow-hidden bg-content text-foreground outline-none",
        phone && "flex-col pb-[276px]",
      )}
    >
      <ShowMessageProvider value={showMessage}>
        <ArtifactEditor
          content={content}
          panel={panel}
          title={title}
          page={page}
          onClose={() => onClose()}
          studio={{
            chatId: chat.sessionId,
            aside,
            turn: { running: chat.running, onSent: chat.sent },
            layout: phone ? "phone" : "wide",
            onSelection,
            versionOrigin: versionLabel,
            waiting: chat.blockedOn,
          }}
        />
      </ShowMessageProvider>
    </section>
  );
}

/** The chip of the newest request ("Slide 1 · cover-title"), what a running turn is about. */
function latestChip(rows: readonly StudioRow[]): string | null {
  for (let index = rows.length - 1; index >= 0; index--) {
    const row = rows[index];
    if (row?.kind === "me") return row.chip;
  }
  return null;
}

function StudioThread({
  rows,
  ready,
  failed,
  peek,
  highlight,
  live,
}: {
  rows: readonly StudioRow[];
  /** Everything it is made of has been read: until then it shows nothing, not pieces. */
  ready: boolean;
  failed: boolean;
  /** The phone's lowered sheet: only the newest row shows. */
  peek: boolean;
  highlight: string | null;
  /** The running turn's strip, after the newest row. */
  live: ReactNode;
}) {
  const { t } = useLingui();
  const list = useRef<HTMLDivElement>(null);
  const count = ready ? rows.length : 0;
  // Newest at the bottom, as a chat reads; a picked version's message is brought into view.
  useEffect(() => {
    const element = list.current;
    if (!element || (count === 0 && !live)) return;
    element.scrollTop = element.scrollHeight;
  }, [count, live]);
  return (
    <div
      ref={list}
      data-testid="studio-thread"
      aria-live="polite"
      className={cn(
        "flex min-h-0 flex-1 flex-col gap-3 px-4 pt-1 pb-2",
        peek ? "justify-end overflow-hidden [&>*:not(:last-child)]:hidden" : "overflow-y-auto",
      )}
    >
      {failed ? (
        <p className="m-auto text-[13px] text-destructive">{t`Could not load this.`}</p>
      ) : ready ? (
        <>
          {rows.map((row) => (
            <ThreadRow key={row.id} row={row} highlighted={highlight === row.id} />
          ))}
          {live}
        </>
      ) : null}
    </div>
  );
}

/** The running studio turn (ADR 0030 "While a turn runs"): the orb, what it is changing or the
 * engine's title for the turn once it has one, and the time it has taken. */
function TurnStrip({
  botId,
  sessionId,
  about,
  waiting,
}: {
  botId: string;
  sessionId: string;
  about: string | null;
  waiting: string | null;
}) {
  const { t } = useLingui();
  const { state } = useActivities(botId);
  const activity =
    state.status === "ready"
      ? state.activities.find((item) => item.chatId === sessionId && item.status === "in_progress")
      : undefined;
  // Until the turn is listed, its clock runs from when this strip appeared.
  const [shownAt] = useState(() => Date.now());
  const now = useNow(true);
  const startedAt = activity ? Date.parse(activity.startedAt) : Number.NaN;
  const elapsed = Number.isNaN(startedAt) ? now - shownAt : serverNow(now) - startedAt;
  const words = waiting || activity?.title || (about ? t`Changing ${about}` : t`Working`);
  return (
    <div
      data-testid="studio-turn"
      className="flex min-w-0 items-center gap-2.5 rounded-[12.5px] bg-content px-3 py-2.5 text-[13px] ring-[0.5px] ring-border"
    >
      <SuperchatOrb size={18} state="working" flies={false} />
      <span className="min-w-0 flex-1 truncate font-[560]" dir="auto">
        {words}
      </span>
      <span className="shrink-0 text-[12px] text-ink-3 tabular-nums">
        {formatClock(Math.max(0, elapsed))}
      </span>
    </div>
  );
}

function ThreadRow({ row, highlighted }: { row: StudioRow; highlighted: boolean }) {
  const { t, i18n } = useLingui();
  const originLabel = useOriginLabel();
  const self = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (highlighted) self.current?.scrollIntoView?.({ block: "center", behavior: "smooth" });
  }, [highlighted]);
  switch (row.kind) {
    case "prelude":
      return <PreludeRow row={row} />;
    case "me":
      return (
        <div
          ref={self}
          data-testid="studio-request"
          className={cn(
            "max-w-[88%] self-end rounded-[14px] rounded-ee-[4px] bg-bubble px-[11px] py-[7px] text-[13.5px] leading-[1.4] text-foreground motion-safe:transition-shadow",
            highlighted && "ring-2 ring-tint",
          )}
        >
          {row.chip ? (
            <span className="mb-[3px] flex items-center gap-1 text-[11px] text-tint-ink">
              <CornerDownRight size={11} aria-hidden="true" />
              {row.chip}
            </span>
          ) : null}
          <span className="whitespace-pre-wrap" dir="auto">
            {row.text}
          </span>
        </div>
      );
    case "ai":
      return (
        <p
          data-testid="studio-reply"
          dir="auto"
          className="m-0 text-[13.5px] leading-normal whitespace-pre-wrap text-foreground"
        >
          {row.text}
        </p>
      );
    case "note":
      return (
        <div
          data-testid="studio-note"
          className="flex items-start gap-[7px] text-[12px] leading-[1.4] text-ink-2"
        >
          <History size={13} aria-hidden="true" className="mt-px shrink-0" />
          <span>
            {row.notice === "queued"
              ? t`Finishing another file first`
              : row.origin === "update"
                ? t`A live update saved v${row.version ?? ""} while you were editing`
                : t`Super Chat saved v${row.version ?? ""} while you were editing`}
          </span>
        </div>
      );
    case "hand":
      return (
        <div
          data-testid="studio-hand-edit"
          className="flex items-start gap-[7px] text-[12px] leading-[1.4] text-ink-2"
        >
          <Pencil size={13} aria-hidden="true" className="mt-px shrink-0" />
          <span dir="auto">
            {row.count > 1
              ? t`You changed ${row.element} · ${row.count} edits · v${row.from} → v${row.to}`
              : row.what}
          </span>
        </div>
      );
    case "version":
      return (
        <div
          data-testid="studio-version"
          className="flex items-center gap-2 text-[11px] text-ink-3"
        >
          <span aria-hidden="true" className="h-px flex-1 bg-separator" />
          <span className="rounded-[4px] border border-border bg-content px-[5px] font-mono text-[10.5px] text-foreground">
            v{row.version}
          </span>
          <span>
            {originLabel(row.origin)} · {formatDayAndTime(row.createdAt, i18n.locale)}
          </span>
          <span aria-hidden="true" className="h-px flex-1 bg-separator" />
        </div>
      );
  }
}

/** What made a version in Super Chat (ADR 0030): where and when, the person's ask, and the
 * hand-back's first paragraph, with the rest one click away here or in Super Chat. */
function PreludeRow({ row }: { row: Extract<StudioRow, { kind: "prelude" }> }) {
  const { t, i18n } = useLingui();
  const [open, setOpen] = useState(false);
  const showMessage = useShowMessage();
  const asked = splitElementAsk(row.ask)?.rest ?? row.ask;
  const handback = row.handback.trim();
  const first = handback.split(/\n\s*\n/)[0] ?? "";
  const more = first !== handback || first.length > 240;
  const when = formatDayAndTime(row.askedAt, i18n.locale);
  return (
    <div data-testid="studio-prelude" className="flex flex-col gap-2.5">
      <div className="flex items-center gap-1.5 text-[11px] text-ink-3">
        <span>{when ? t`In Super Chat · ${when}` : t`In Super Chat`}</span>
        <span aria-hidden="true" className="h-px flex-1 bg-separator" />
      </div>
      {asked ? (
        <div
          data-testid="studio-prelude-ask"
          dir="auto"
          className="max-w-[88%] self-end rounded-[14px] rounded-ee-[4px] bg-bubble px-[11px] py-[7px] text-[13.5px] leading-[1.4] whitespace-pre-wrap text-foreground"
        >
          {asked}
        </div>
      ) : null}
      {handback ? (
        <div
          data-testid="studio-prelude-handback"
          className={cn(
            "text-[13.5px] leading-normal text-foreground [&_.sc-chat-markdown]:text-[13.5px] [&_.sc-chat-markdown]:leading-normal",
            !open && "line-clamp-3",
          )}
        >
          <ChatMarkdown>{open ? handback : first}</ChatMarkdown>
        </div>
      ) : null}
      {more || (showMessage && row.itemId) ? (
        <div className="-mt-1 flex gap-3 text-[12px]">
          {more ? (
            <button
              type="button"
              aria-expanded={open}
              onClick={() => setOpen((value) => !value)}
              className="text-tint-ink hover:underline"
            >
              {open ? t`Less` : t`More`}
            </button>
          ) : null}
          {showMessage && row.itemId ? (
            <button
              type="button"
              data-testid="studio-prelude-show"
              onClick={() => row.itemId && showMessage(row.itemId)}
              className="text-tint-ink hover:underline"
            >
              {t`Show in Super Chat ↗`}
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
