import { Trans } from "@lingui/react/macro";
import { Button } from "@superchat/ui-web";

/** The Muse's settings panel: clearing the Conversation. */
export function BotSettingsPanel({ onClear }: { onClear: () => void }) {
  return (
    <Button
      variant="ghost"
      size="sm"
      className="-ms-2.5 text-destructive hover:text-destructive"
      onClick={onClear}
    >
      <Trans>Clear conversation</Trans>
    </Button>
  );
}
