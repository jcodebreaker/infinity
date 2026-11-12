import { t } from "@lingui/core/macro";
import type { SlashActionId } from "@superchat/core";

export function slashActionLabel(id: SlashActionId) {
  switch (id) {
    case "chat-settings":
      return t`Chat Settings`;
    case "settings-general":
      return t`Settings: General`;
  }
}
