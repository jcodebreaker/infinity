import type {
  ArtifactVersion,
  MessageBlock,
  StudioPrelude,
  ThreadMessage,
} from "@superchat/contracts";
import { splitElementAsk } from "../editor/html-ask";

// A file's studio thread (ADR 0024, 0030): the studio session's transcript merged with the file's
// version list by time. Requests and replies come from the transcript, as do the engine's notices
// (`notice` blocks with a structured kind: a hand edit, a version saved elsewhere, a stale save);
// version markers come from the versions; the prelude (what made a version in Super Chat: the
// person's ask and the hand-back) comes with the session and sits just before its version.

/** Who made a version, in the studio's five words. */
export type VersionOrigin = "chat" | "superchat" | "you" | "restored" | "updated";

type Notice = Extract<MessageBlock, { kind: "notice" }>;

export type StudioRow =
  /** The person's request, with the element chip it was sent with. */
  | {
      kind: "me";
      id: string;
      text: string;
      chip: string | null;
      /** The one slide of a deck the request was about. */
      slide: number | null;
      at: number;
    }
  /** The assistant's reply. */
  | { kind: "ai"; id: string; text: string; at: number }
  /** A version saved elsewhere while the studio was editing, or a turn waiting for a slot. */
  | {
      kind: "note";
      id: string;
      notice: "stale" | "queued";
      version: number | null;
      origin: "superchat" | "update";
      at: number;
    }
  /** Edits by hand: one version, or a run of consecutive ones on one element. */
  | {
      kind: "hand";
      id: string;
      /** The engine's plain words for one edit ("You made the title larger"). */
      what: string;
      elementId: string | null;
      /** "the title": what a folded run names. */
      element: string | null;
      count: number;
      from: number;
      to: number;
      at: number;
    }
  /** A version was saved. */
  | {
      kind: "version";
      id: string;
      version: number;
      origin: VersionOrigin;
      createdAt: string;
      at: number;
    }
  /** A version made in Super Chat: the person's ask there and the Assistant's hand-back. */
  | {
      kind: "prelude";
      id: string;
      ask: string;
      handback: string;
      /** The Conversation message "Show in Super Chat" scrolls to. */
      itemId: string | null;
      /** When the person asked (ISO). */
      askedAt: string;
      at: number;
    };

/** Who made `version`, seen from the studio session `studioId`. */
export function versionOrigin(version: ArtifactVersion, studioId: string | null): VersionOrigin {
  if (version.origin === "manual") return "you";
  if (version.origin === "restore") return "restored";
  if (version.origin === "update") return "updated";
  return version.fromStudio || (studioId !== null && version.chatId === studioId)
    ? "chat"
    : "superchat";
}

const time = (iso: string): number => {
  const at = Date.parse(iso);
  return Number.isNaN(at) ? 0 : at;
};

function messageText(message: ThreadMessage): string {
  return message.blocks
    .map((block) => (block.kind === "text" ? block.text : ""))
    .join("\n")
    .trim();
}

const noticeOf = (message: ThreadMessage): Notice | null =>
  message.blocks.find((block): block is Notice => block.kind === "notice") ?? null;

/**
 * The studio thread, oldest first. A version saved by this chat sits after the reply of the turn
 * that saved it (the turn's request is its `itemId`); every other version at the time it was
 * saved, its hand edit just before it, and its prelude (made in Super Chat) just before that.
 * Consecutive hand edits to one element read as one row.
 */
export function studioThread(
  messages: readonly ThreadMessage[],
  versions: readonly ArtifactVersion[],
  studioId: string | null,
  prelude: readonly StudioPrelude[] = [],
): StudioRow[] {
  const rows: StudioRow[] = [];
  const replyAfter = new Map<string, number>();
  const savedAt = new Map(versions.map((version) => [version.version, time(version.createdAt)]));
  let lastRequest: string | null = null;
  for (const message of messages) {
    const at = time(message.createdAt);
    if (message.role === "system") {
      const notice = noticeOf(message);
      if (notice?.notice === "hand_edit" && notice.version !== undefined) {
        rows.push({
          kind: "hand",
          id: `hand:${message.id}`,
          what: notice.what ?? "",
          elementId: notice.elementId ?? null,
          element: notice.element ?? null,
          count: 1,
          from: notice.version,
          to: notice.version,
          // Just before the version it made, whose marker follows it.
          at: (savedAt.get(notice.version) ?? at) - 0.5,
        });
      } else if (notice?.notice === "stale" || notice?.notice === "queued") {
        rows.push({
          kind: "note",
          id: message.id,
          notice: notice.notice,
          version: notice.version ?? null,
          origin: notice.origin ?? "superchat",
          at,
        });
      }
      continue;
    }
    const text = messageText(message);
    if (!text) continue;
    if (message.role === "user") {
      const ask = splitElementAsk(text);
      rows.push({
        kind: "me",
        id: message.id,
        text: ask ? ask.rest : text,
        chip: ask?.label ?? null,
        slide: ask?.slide ?? null,
        at,
      });
      lastRequest = message.id;
    } else {
      rows.push({ kind: "ai", id: message.id, text, at });
      if (lastRequest !== null && !replyAfter.has(lastRequest)) replyAfter.set(lastRequest, at);
    }
  }
  for (const made of prelude) {
    if (!made.ask && !made.handback) continue;
    rows.push({
      kind: "prelude",
      id: `prelude:${made.version}`,
      ask: made.ask,
      handback: made.handback,
      itemId: made.itemId,
      askedAt: made.at,
      // Just before its version's marker (and any hand edit there); on its own time otherwise.
      at: (savedAt.get(made.version) ?? time(made.at)) - 0.75,
    });
  }
  for (const version of [...versions].reverse()) {
    const origin = versionOrigin(version, studioId);
    let at = time(version.createdAt);
    if (origin === "chat" && version.itemId) {
      const reply = replyAfter.get(version.itemId);
      if (reply !== undefined && reply >= at) at = reply + 0.5;
    }
    rows.push({
      kind: "version",
      id: version.id,
      version: version.version,
      origin,
      createdAt: version.createdAt,
      at,
    });
  }
  // Stable: a request before its own reply, a hand edit before its marker.
  const sorted = rows
    .map((row, index) => ({ row, index }))
    .sort((a, b) => a.row.at - b.row.at || a.index - b.index)
    .map(({ row }) => row);
  return collapseHandEdits(sorted);
}

/** Folds `hand, marker, hand, marker` on one element into one hand row and its last marker. */
function collapseHandEdits(rows: StudioRow[]): StudioRow[] {
  const out: StudioRow[] = [];
  for (const row of rows) {
    const marker = out.at(-1);
    const previous = out.at(-2);
    if (
      row.kind === "hand" &&
      row.elementId !== null &&
      marker?.kind === "version" &&
      marker.origin === "you" &&
      previous?.kind === "hand" &&
      previous.elementId === row.elementId &&
      marker.version === previous.to
    ) {
      out.pop();
      out[out.length - 1] = { ...previous, count: previous.count + row.count, to: row.to };
      continue;
    }
    out.push(row);
  }
  return out;
}
