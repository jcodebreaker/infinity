// The engine's transcript page -> Superchat's `ThreadMessagePage`. Mechanical only: snake_case to
// camelCase and engine block types onto the chat's existing block types. Every rule (de-duping,
// dropping system notices, error codes) is the engine's
// (engine/omnigent/superchat/transcript/). The wording of the file and secure-entry
// fallbacks is Superchat's.
import type {
  MessageArtifact,
  MessageBlock,
  ReplyCardBlock,
  ReplyEvent,
  ThreadMessage,
  ThreadMessagePage,
} from "@superchat/contracts";
import type { OmnigentStreamEvent } from "./client/core.js";
import type {
  OmnigentMessageArtifact,
  OmnigentTranscriptBlock,
  OmnigentTranscriptMessage,
  OmnigentTranscriptPage,
} from "./client/transcript.js";

function epochSecondsToIso(epochSeconds: number | null | undefined): string {
  return new Date((epochSeconds ?? 0) * 1000).toISOString();
}

function fileCard(block: Extract<OmnigentTranscriptBlock, { type: "file" }>): ReplyCardBlock {
  const label = block.title || block.name;
  const revision = block.version && block.version > 1 ? ` (version ${block.version})` : "";
  return {
    kind: "reply_card",
    card: "file",
    id: `artifact:${block.artifact_id}`,
    data: {
      name: block.name,
      artifactId: block.artifact_id,
      ...(block.kind ? { kind: block.kind } : {}),
      ...(block.size !== undefined ? { size: block.size } : {}),
      ...(block.version !== undefined ? { version: block.version } : {}),
      ...(block.versions !== undefined ? { versions: block.versions } : {}),
      ...(block.by === "user" ? { byYou: true } : {}),
    },
    fallback: `Saved **${label}**${revision}. Open it in Superchat's Library.`,
  };
}

function secureEntryCard(
  block: Extract<OmnigentTranscriptBlock, { type: "secure_entry" }>,
): ReplyCardBlock {
  return {
    kind: "reply_card",
    card: "secure_entry",
    data: {
      requestId: block.request_id,
      name: block.name,
      site: block.site,
      ...(block.reason ? { reason: block.reason } : {}),
    },
    fallback: `Superchat needs a login for ${block.site}. Open this chat in Superchat to enter it securely.`,
  };
}

function mapBlock(block: OmnigentTranscriptBlock): MessageBlock {
  switch (block.type) {
    case "text":
      return { kind: "text", text: block.text };
    case "card": {
      // The engine's card id: what an answer to the card names.
      const id = block.card_id || block.card.id;
      return {
        kind: "reply_card",
        card: block.card.card,
        ...(id ? { id } : {}),
        ...(block.card.title ? { title: block.card.title } : {}),
        data: block.card.data,
        fallback: block.card.fallback,
        ...(block.pending ? { pending: true } : {}),
      };
    }
    case "helper":
      return {
        kind: "helper",
        helperId: block.session_id,
        title: block.title,
        ...(block.status ? { status: block.status } : {}),
      };
    case "file":
      return fileCard(block);
    case "secure_entry":
      return secureEntryCard(block);
    case "error":
      return { kind: "error", code: block.code, ...(block.level ? { level: block.level } : {}) };
    case "notice":
      return {
        kind: "notice",
        notice: block.kind,
        ...(block.version !== undefined ? { version: block.version } : {}),
        ...(block.element_id ? { elementId: block.element_id } : {}),
        ...(block.element ? { element: block.element } : {}),
        ...(block.what ? { what: block.what } : {}),
        ...(block.origin ? { origin: block.origin } : {}),
      };
  }
}

/** The artifact (and version, when the card names one) a `render_card file` block points at. */
function fileCardTarget(
  block: OmnigentTranscriptBlock,
): { artifactId: string; version: number | undefined } | null {
  if (block.type !== "card" || block.card.card !== "file") return null;
  const data = block.card.data as { artifactId?: unknown; version?: unknown } | null;
  if (typeof data?.artifactId !== "string" || !data.artifactId) return null;
  return {
    artifactId: data.artifactId,
    version: typeof data.version === "number" ? data.version : undefined,
  };
}

/**
 * `artifact_save` already shows the saved file as a card (with its size). A Muse that then also
 * calls `render_card file` for the same artifact version would show it twice, so within one
 * assistant turn (the run of assistant messages after a user message) that second card is
 * dropped, and its message with it when it holds nothing else.
 */
function dropRepeatedFileCards(
  messages: OmnigentTranscriptPage["data"],
): OmnigentTranscriptPage["data"] {
  let shown = new Set<string>(); // `id` and `id@version` of every artifact shown this turn
  const kept: OmnigentTranscriptPage["data"] = [];
  for (const message of messages) {
    if (message.role !== "assistant") {
      shown = new Set();
      kept.push(message);
      continue;
    }
    const blocks = message.blocks.filter((block) => {
      if (block.type === "file") {
        shown.add(block.artifact_id);
        shown.add(`${block.artifact_id}@${block.version ?? 1}`);
        return true;
      }
      const target = fileCardTarget(block);
      if (!target) return true;
      return !shown.has(
        target.version === undefined ? target.artifactId : `${target.artifactId}@${target.version}`,
      );
    });
    if (blocks.length === message.blocks.length) kept.push(message);
    else if (blocks.length > 0) kept.push({ ...message, blocks });
  }
  return kept;
}

function mapMessageArtifact(
  artifact: OmnigentMessageArtifact | null | undefined,
): MessageArtifact | null | undefined {
  if (artifact === undefined) return undefined;
  if (artifact === null) return null;
  return {
    artifactId: artifact.artifact_id,
    name: artifact.name,
    version: artifact.version ?? null,
    how: artifact.how,
  };
}

/** One transcript message of chat `chatId` (`seq` is its place on the page). */
export function mapTranscriptMessage(
  chatId: string,
  message: OmnigentTranscriptMessage,
  seq = 0,
): ThreadMessage {
  const artifact = mapMessageArtifact(message.artifact);
  return {
    id: message.id,
    threadId: chatId,
    seq,
    role: message.role === "assistant" ? "bot" : message.role === "system" ? "system" : "user",
    blocks: message.blocks.map(mapBlock),
    createdAt: epochSecondsToIso(message.created_at),
    ...(message.delivered ? { delivered: message.delivered } : {}),
    ...(artifact !== undefined ? { artifact } : {}),
    ...(typeof message.activity_id === "string" && message.activity_id
      ? { activityId: message.activity_id }
      : {}),
    ...(message.proactive
      ? {
          proactive: {
            kind: message.proactive.kind,
            ...(message.proactive.topic_key ? { topicKey: message.proactive.topic_key } : {}),
            urgency: message.proactive.urgency,
          },
        }
      : {}),
  };
}

/** The turn-ending `response.*` frames: the turn's text is stored or dropped. */
const TERMINAL_RESPONSE_FRAMES = new Set([
  "response.completed",
  "response.failed",
  "response.cancelled",
  "response.incomplete",
]);

/** The reply being written, from a frame of `/v1/sessions/{id}/stream`: each text delta (the
 * in-flight text the engine replays on (re)connect arrives as one delta of everything so far),
 * and `textReset` when that text is stored as a message or the turn ends. Null for any other
 * frame. */
export function mapReplyFrame(frame: OmnigentStreamEvent): ReplyEvent | null {
  const body = (typeof frame.data === "object" && frame.data !== null ? frame.data : frame) as {
    delta?: unknown;
    item?: { type?: unknown } | null;
  };
  if (frame.type === "response.output_text.delta") {
    return typeof body.delta === "string" && body.delta
      ? { kind: "text", delta: body.delta }
      : null;
  }
  if (frame.type === "response.output_item.done") {
    return body.item?.type === "message" ? { kind: "textReset" } : null;
  }
  return TERMINAL_RESPONSE_FRAMES.has(frame.type) ? { kind: "textReset" } : null;
}

/** One transcript page for chat `chatId`. The engine has already redacted its secrets. */
export function mapTranscriptPage(chatId: string, page: OmnigentTranscriptPage): ThreadMessagePage {
  const messages: ThreadMessage[] = dropRepeatedFileCards(page.data).map((message, seq) =>
    mapTranscriptMessage(chatId, message, seq),
  );
  return {
    threadId: chatId,
    messages,
    olderCursor: null,
    olderItemCursor: page.has_more ? page.older_cursor : null,
    ...(page.live === undefined ? {} : { running: page.live }),
    ...(page.reset
      ? {
          reset: {
            itemId: page.reset.item_id,
            createdAt: epochSecondsToIso(page.reset.created_at),
          },
        }
      : { reset: null }),
  };
}
