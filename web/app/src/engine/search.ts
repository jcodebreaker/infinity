import { searchOmnigentFamily } from "@superchat/adapters/omnigent/browser";
import { DEFAULT_MUSE_NAME, type SearchHit } from "@superchat/contracts";
import { snippetAroundMatch } from "@superchat/core";
import { engineClient, museSessionId, NO_EMAIL } from "./client";
import { os } from "./context";

const SEARCH_LIMIT = 25;

/** Full-text search over the Muse's Conversation, ranked by the engine
 * (`GET /v1/sessions/{id}/items/search?scope=family`). Hits outside the Conversation itself have
 * nowhere to open, so they are left out. */
async function searchMuse(q: string): Promise<SearchHit[]> {
  const query = q.trim();
  if (!query) return [];
  const root = await museSessionId();
  const messages = await searchOmnigentFamily(engineClient(), NO_EMAIL, root, query, SEARCH_LIMIT);
  return messages
    .filter((message) => message.sessionId === root)
    .map((message) => ({
      kind: "message",
      botId: root,
      botName: DEFAULT_MUSE_NAME,
      title: DEFAULT_MUSE_NAME,
      snippet: snippetAroundMatch(message.text, query),
      messageId: message.id,
      seq: 0,
    }));
}

export const searchRouter = {
  search: {
    query: os.search.query.handler(async ({ input }) => ({ hits: await searchMuse(input.q) })),
  },
};
