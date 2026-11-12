import { Trans } from "@lingui/react/macro";
import type { Bot } from "@superchat/contracts";
import { cn } from "@superchat/ui-web";
import { Check, ChevronRight } from "lucide-react";
import { useState } from "react";
import { SuperchatOrb } from "../../components/ai/orb";
import { MUSE_INSET_GROUP } from "../../components/ui";
import { ApprovalsSettings } from "../../features/approvals";
import { VaultSettings } from "../../features/vault";
import { resetFirstRun } from "./intro";

/**
 * What the person tunes about their Muse, gathered in one place (the old design notes):
 * Superchat is always the orb, with the name the engine gives it.
 */
export function SuperchatSettingsPanel({ bot, userId }: { bot: Bot; userId: string | undefined }) {
  const [replayed, setReplayed] = useState(false);

  return (
    <div data-testid="superchat-settings" className="flex flex-col gap-8 pb-4">
      <div className={cn(MUSE_INSET_GROUP, "flex flex-col items-center px-6 pt-7 pb-5")}>
        <SuperchatOrb size={88} flies={false} />
        <h3 className="mt-4 text-[24px] font-semibold tracking-[-0.02em] text-foreground">
          {bot.name}
        </h3>
      </div>

      <ApprovalsSettings botId={bot.id} />

      <VaultSettings botId={bot.id} />

      <div className={MUSE_INSET_GROUP}>
        <button
          type="button"
          onClick={() => {
            resetFirstRun(userId);
            setReplayed(true);
          }}
          className="flex min-h-[52px] w-full items-center justify-between gap-4 px-4 text-start transition-colors hover:bg-accent/50 active:bg-accent"
        >
          <span className="text-[16px] text-foreground">
            <Trans>Replay the intro</Trans>
          </span>
          {replayed ? (
            <span className="flex items-center gap-1 text-[14px] text-muted-foreground">
              <Check size={15} strokeWidth={2.25} aria-hidden="true" />
              <Trans>Done</Trans>
            </span>
          ) : (
            <ChevronRight
              size={18}
              strokeWidth={2}
              aria-hidden="true"
              className="text-muted-foreground/60"
            />
          )}
        </button>
      </div>
    </div>
  );
}
