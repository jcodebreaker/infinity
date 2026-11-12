import type { OmnigentClientConfig } from "./client/core.js";
import { isSessionNotFoundError } from "./client/core.js";
import { getOmnigentSession } from "./client/sessions.js";

// The ownership rule that lets a client resolve a bare chatId (a Helper's chat) to the
// Conversation (bot) it belongs to before ever trusting it (docs/architecture.md).

export interface ChatOwnership {
  /** The Conversation's bot the chat belongs to. */
  botId: string;
}

export interface OwnedSuperChat {
  botId: string;
  omnigentSessionId: string;
}

export type ChatOwnershipResolver = (chatId: string) => Promise<ChatOwnership | null>;

/**
 * The ownership rule (ADR 0009): `chatId` is a Helper whose family root, as the engine reports it
 * on the session (`superchat.root_id`), is one of the caller's own Conversations. The engine
 * itself answers 404 for a session the caller cannot read. Returns `null`, never throws, for
 * "not yours", so the caller turns that into a `NOT_FOUND` rather than leaking whether a chat
 * exists; any other failure throws.
 *
 * Build one per request and reuse it: each chat's session is read once.
 */
export function createChatOwnershipResolver(
  client: OmnigentClientConfig,
  email: string,
  superChats: OwnedSuperChat[],
): ChatOwnershipResolver {
  const owners = new Map(superChats.map((chat) => [chat.omnigentSessionId, chat]));
  const resolved = new Map<string, Promise<ChatOwnership | null>>();
  const resolve = async (chatId: string): Promise<ChatOwnership | null> => {
    const session = await getOmnigentSession(client, email, chatId).catch((error: unknown) => {
      if (isSessionNotFoundError(error)) return null;
      throw error;
    });
    const family = session?.superchat;
    if (!family?.root_id || family.kind !== "helper") return null;
    const owner = owners.get(family.root_id);
    return owner ? { botId: owner.botId } : null;
  };
  return (chatId) => {
    let found = resolved.get(chatId);
    if (!found) {
      found = resolve(chatId);
      resolved.set(chatId, found);
    }
    return found;
  };
}

/** One-off :func:`createChatOwnershipResolver` lookup, for a single chat per request. */
export function resolveChatOwnership(
  client: OmnigentClientConfig,
  email: string,
  superChats: OwnedSuperChat[],
  chatId: string,
): Promise<ChatOwnership | null> {
  return createChatOwnershipResolver(client, email, superChats)(chatId);
}
