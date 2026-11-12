import { Trans, useLingui } from "@lingui/react/macro";
import type { Bot } from "@superchat/contracts";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@superchat/ui-web";
import { useState } from "react";

/** Each dialog is mounted only while open, so `open` is always true and the
 * parent unmounts it from `onCancel`. Escape and backdrop presses are ignored
 * while a request is in flight. */
function closeUnlessBusy(busy: boolean, onCancel: () => void) {
  return (open: boolean) => {
    if (!open && !busy) onCancel();
  };
}

export function ClearConversationDialog({
  bot,
  onCancel,
  onConfirm,
}: {
  bot: Pick<Bot, "name">;
  onCancel: () => void;
  onConfirm: () => Promise<void>;
}) {
  const { t } = useLingui();
  const [clearing, setClearing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  return (
    <AlertDialog open onOpenChange={closeUnlessBusy(clearing, onCancel)}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle className="break-words">
            <Trans>Clear {bot.name}’s conversation?</Trans>
          </AlertDialogTitle>
          <AlertDialogDescription>
            <Trans>superchat starts fresh. You can still show earlier messages.</Trans>
          </AlertDialogDescription>
        </AlertDialogHeader>
        {error ? <p className="text-[13.5px] text-destructive">{error}</p> : null}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={clearing}>
            <Trans>Cancel</Trans>
          </AlertDialogCancel>
          <AlertDialogAction
            variant="destructive"
            disabled={clearing}
            onClick={() => {
              setClearing(true);
              setError(null);
              void onConfirm().catch((err: unknown) => {
                setError(err instanceof Error ? err.message : t`Could not clear conversation`);
                setClearing(false);
              });
            }}
          >
            {clearing ? <Trans>Clearing…</Trans> : <Trans>Clear</Trans>}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
