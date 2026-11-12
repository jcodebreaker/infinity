import { useLingui } from "@lingui/react/macro";
import { ORPCError } from "@orpc/client";
import type { Bot } from "@superchat/contracts";
import { rpc } from "../../../lib/rpc";
import { ClearConversationDialog } from "../../shell/dialogs";

/** Confirms and performs clearing the Conversation. It is cleared on the engine (every client then
 * refetches on `chat.reset`); the engine refuses while Superchat is working. */
export function ClearConversationHost({
  bot,
  onClose,
  onCleared,
}: {
  bot: Bot;
  onClose: () => void;
  /** The Conversation was cleared: read it again. */
  onCleared: () => void;
}) {
  const { t } = useLingui();
  return (
    <ClearConversationDialog
      bot={bot}
      onCancel={() => onClose()}
      onConfirm={async () => {
        try {
          await rpc.chats.reset({ botId: bot.id });
        } catch (error) {
          if (error instanceof ORPCError && error.code === "CONFLICT") {
            throw new Error(t`Wait for superchat to finish, then clear.`);
          }
          throw error;
        }
        onCleared();
        onClose();
      }}
    />
  );
}
