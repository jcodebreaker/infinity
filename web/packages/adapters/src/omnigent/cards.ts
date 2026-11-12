// Reply cards on the Omnigent side. The engine projects `render_card` calls into card blocks
// (engine/omnigent/superchat/transcript/); the only rule left here is that a card never
// carries a secret.
import type { MessageBlock, ReplyCardBlock } from "@superchat/contracts";
import { redactSecrets } from "@superchat/core";

/** A card never carries a secret: one that does degrades to its redacted markdown fallback. */
export function redactReplyCard(block: ReplyCardBlock, secrets: string[]): MessageBlock {
  const serialized = JSON.stringify(block);
  if (redactSecrets(serialized, secrets) === serialized) return block;
  return { kind: "text", text: redactSecrets(block.fallback, secrets) };
}
