import { useLingui } from "@lingui/react/macro";
import { cn } from "@superchat/ui-web";
import type { ReactNode } from "react";
import { useAsks } from "../../../features/approvals";
import { useOutboxUnread } from "../../../features/outbox";

/** The places the dock opens, each as a large sheet over the home. */
export type DockKey = "inbox" | "apps" | "automations" | "files";

const ICON = {
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.7,
  strokeLinecap: "round",
  strokeLinejoin: "round",
  width: 24,
  height: 24,
  viewBox: "0 0 24 24",
  "aria-hidden": true,
} as const;

const GLYPHS: Record<DockKey | "conversation" | "settings" | "topics", ReactNode> = {
  conversation: (
    <svg {...ICON}>
      <path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z" />
    </svg>
  ),
  topics: (
    <svg {...ICON}>
      <path d="M9 18h6M10 21h4" />
      <path d="M12 3a6 6 0 0 0-3.6 10.8c.6.5 1 1.2 1 2V16h5.2v-.2c0-.8.4-1.5 1-2A6 6 0 0 0 12 3z" />
    </svg>
  ),
  inbox: (
    <svg {...ICON}>
      <path d="M3 13h5l1.5 3h5l1.5-3h5" />
      <path d="M5.5 5h13L21 13v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-5z" />
    </svg>
  ),
  apps: (
    <svg {...ICON}>
      <rect x="4" y="4" width="6.5" height="6.5" rx="1.6" />
      <rect x="13.5" y="4" width="6.5" height="6.5" rx="1.6" />
      <rect x="4" y="13.5" width="6.5" height="6.5" rx="1.6" />
      <rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.6" />
    </svg>
  ),
  automations: (
    <svg {...ICON}>
      <rect x="3.5" y="5" width="17" height="15" rx="2.5" />
      <path d="M3.5 10h17M8 3v4M16 3v4" />
      <path d="M8 14h2M12 14h2M16 14h0.5M8 17h2M12 17h2" />
    </svg>
  ),
  files: (
    <svg {...ICON}>
      <path d="M3.5 7a2 2 0 0 1 2-2h4l2 2.5h7a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z" />
    </svg>
  ),
  settings: (
    <svg {...ICON} width={22} height={22} strokeWidth={1.6}>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
    </svg>
  ),
};

/** The dock's Topics button: it opens the Topics panel beside the dock, not a sheet. */
export type DockTopics = {
  open: boolean;
  /** A topic has a fresh "New" line: the lightbulb carries a green dot. */
  fresh: boolean;
  onToggle: () => void;
};

const BUTTON =
  "relative grid size-[52px] place-items-center rounded-2xl text-foreground transition-colors hover:bg-foreground/10 focus-visible:outline-2 focus-visible:outline-ring";

/**
 * The floating glass dock: the Conversation, Topics, Inbox, Library, Automations and Files. Inbox
 * carries a dot while something waits on the person; the Conversation carries one while it
 * holds messages Superchat wrote on its own that the person has not seen (ADR 0036); Topics
 * carries a green one while a topic has news (ADR 0035).
 */
export function Dock({
  active,
  inboxWaiting,
  unread = 0,
  onSelect,
  topics,
  onConversation,
  conversationShown = true,
}: {
  active: DockKey | null;
  inboxWaiting: boolean;
  /** Proactive messages not seen yet. */
  unread?: number;
  onSelect: (key: DockKey) => void;
  topics?: DockTopics;
  /** Back to the Conversation (closes whatever the dock opened); without it there is no button. */
  onConversation?: () => void;
  /** Whether the Conversation is the screen (not Home): its button reads pressed only then. */
  conversationShown?: boolean;
}) {
  const { t } = useLingui();
  const onConversationPage = conversationShown && active === null;
  const items: { key: DockKey; label: string }[] = [
    { key: "inbox", label: t`Inbox` },
    { key: "apps", label: t`Library` },
    { key: "automations", label: t`Automations` },
    { key: "files", label: t`Files` },
  ];
  return (
    <nav
      data-testid="dock"
      aria-label={t`Main`}
      className="sc-glass flex flex-row gap-2.5 rounded-3xl p-3 md:flex-col"
    >
      {onConversation ? (
        <button
          type="button"
          aria-label={t`Conversation`}
          title={t`Conversation`}
          aria-pressed={onConversationPage}
          onClick={onConversation}
          className={cn(BUTTON, onConversationPage && "bg-foreground/15")}
        >
          {GLYPHS.conversation}
          {unread > 0 ? (
            <span
              data-testid="dock-conversation-dot"
              className="absolute top-[9px] right-[9px] size-[9px] rounded-full border-2 border-popover bg-success"
            >
              <span className="sr-only">{t`New messages`}</span>
            </span>
          ) : null}
        </button>
      ) : null}
      {topics ? (
        <button
          type="button"
          data-testid="dock-topics"
          aria-label={topics.fresh ? t`Topics, something new` : t`Topics`}
          title={t`Topics`}
          aria-pressed={topics.open}
          onClick={topics.onToggle}
          className={cn(BUTTON, topics.open && "bg-foreground/15")}
        >
          {GLYPHS.topics}
          {topics.fresh && !topics.open ? (
            <span
              aria-hidden="true"
              className="absolute top-[9px] right-[9px] size-[9px] rounded-full border-2 border-popover bg-ok"
            >
              <span className="absolute -inset-1 rounded-full border-2 border-ok opacity-70 motion-safe:animate-ping motion-reduce:hidden" />
            </span>
          ) : null}
        </button>
      ) : null}
      {items.map((item) => (
        <button
          key={item.key}
          type="button"
          aria-label={item.label}
          title={item.label}
          aria-pressed={active === item.key}
          onClick={() => onSelect(item.key)}
          className={cn(BUTTON, active === item.key && "bg-foreground/15")}
        >
          {GLYPHS[item.key]}
          {item.key === "inbox" && inboxWaiting ? (
            <span
              data-testid="dock-inbox-dot"
              className="absolute top-[9px] right-[9px] size-[9px] rounded-full border-2 border-popover bg-warning"
            >
              <span className="sr-only">{t`Waiting on you`}</span>
            </span>
          ) : null}
        </button>
      ))}
    </nav>
  );
}

/** The gear at the bottom-left that opens Settings. */
export function SettingsButton({ onClick }: { onClick: () => void }) {
  const { t } = useLingui();
  return (
    <button
      type="button"
      aria-label={t`Settings`}
      title={t`Settings`}
      onClick={onClick}
      className="grid size-11 place-items-center rounded-xl text-foreground transition-colors hover:bg-foreground/10 focus-visible:outline-2 focus-visible:outline-ring"
    >
      {GLYPHS.settings}
    </button>
  );
}

/** The dock with its Inbox dot wired to the open Asks and its Conversation dot to the unseen
 * proactive messages. */
export function ConnectedDock({
  botId,
  active,
  onSelect,
  topics,
  onConversation,
  conversationShown,
}: {
  botId: string;
  active: DockKey | null;
  onSelect: (key: DockKey) => void;
  topics?: DockTopics;
  onConversation?: () => void;
  conversationShown?: boolean;
}) {
  const { asks } = useAsks(botId);
  const { count } = useOutboxUnread(botId);
  return (
    <Dock
      active={active}
      inboxWaiting={asks.length > 0}
      unread={count}
      onSelect={onSelect}
      onConversation={onConversation}
      conversationShown={conversationShown}
      topics={topics}
    />
  );
}
