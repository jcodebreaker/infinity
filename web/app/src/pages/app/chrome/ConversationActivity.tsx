import { useLingui } from "@lingui/react/macro";
import { Clock } from "lucide-react";
import { useEffect, useState } from "react";
import { useActivityFocus } from "../../../features/activity";
import { HomeActivity } from "./HomeActivity";

/**
 * The Activity card in the Conversation: a small glass pill bottom-right that opens the same
 * card the home shows, so it never sits over the transcript unless asked for. "Show details" on
 * a reply's work strip opens it too (the panel then brings that Helper's row into view).
 */
export function ConversationActivity({ botId }: { botId: string }) {
  const { t } = useLingui();
  const [open, setOpen] = useState(false);
  const focus = useActivityFocus();
  useEffect(() => {
    if (focus) setOpen(true);
  }, [focus]);
  if (!open) {
    return (
      <button
        type="button"
        data-testid="activity-pill"
        onClick={() => setOpen(true)}
        className="sc-glass inline-flex h-11 items-center gap-2 rounded-xl px-3.5 text-[14px] font-medium text-foreground transition-colors hover:bg-foreground/10 focus-visible:outline-2 focus-visible:outline-ring"
      >
        <Clock size={18} strokeWidth={1.7} aria-hidden="true" />
        {t`Activity`}
      </button>
    );
  }
  return <HomeActivity botId={botId} className="shadow-float" onHide={() => setOpen(false)} />;
}
