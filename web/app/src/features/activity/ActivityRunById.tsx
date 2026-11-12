import type { Activity } from "@superchat/contracts";
import { useEffect, useRef, useState } from "react";
import type { ActivityWire } from "./ActivityRunDialog";
import { ActivityRunDialog } from "./ActivityRunDialog";
import { LIVE_ACTIVITY_WIRE } from "./useActivities";

/** A Helper's Activity id: the Helper's own chat id under its kind. */
export const helperActivityId = (chatId: string) => `sub_agent:${chatId}`;

/**
 * Opens one Helper's run in the Activity run page when the caller knows only its chat (an
 * Automation's run history): the same dialog the Activity panel opens, so steps and outcome look
 * the same. Renders nothing until the Activity has loaded, or when it cannot be read.
 */
export function ActivityRunById({
  botId,
  chatId,
  onClose,
  wire = LIVE_ACTIVITY_WIRE,
}: {
  botId: string;
  chatId: string;
  onClose: () => void;
  wire?: ActivityWire;
}) {
  const [activity, setActivity] = useState<Activity | null>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    let cancelled = false;
    setActivity(null);
    wire
      .get({ botId, activityId: helperActivityId(chatId) })
      .then((next) => !cancelled && setActivity(next))
      .catch(() => !cancelled && close.current());
    return () => {
      cancelled = true;
    };
  }, [botId, chatId, wire]);
  return (
    <ActivityRunDialog
      botId={botId}
      wire={wire}
      activity={activity}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    />
  );
}
