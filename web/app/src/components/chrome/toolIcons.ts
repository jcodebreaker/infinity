import type { ActivitySource } from "@superchat/contracts";
import type { ToolIconKey } from "@superchat/core";
import type { LucideIcon } from "lucide-react";
import {
  Bot,
  CalendarClock,
  Circle,
  Fingerprint,
  Globe,
  History,
  MessageCircle,
  NotebookPen,
  Pencil,
  Search,
} from "lucide-react";

// A Step's icon (docs/helpers.md "The activity panel"; the owner's "always show
// icons, never the tool name"): one lucide icon per `ToolIconKey`
// (web/packages/core/src/tool-presentation.ts), reusing `Fingerprint` from the Memory tab
// (ContextPanel.tsx) so "memory" reads the same way in both places.
export const TOOL_ICON_COMPONENT: Record<ToolIconKey, LucideIcon> = {
  search: Search,
  web: Globe,
  memory: Fingerprint,
  helper: Bot,
  history: History,
  edit: Pencil,
  generic: Circle,
};

/** An Activity's icon by where it came from (web/packages/contracts/src/rpc/activity.ts
 * `ActivitySource`): a Conversation turn, background work, upkeep, a scheduled
 * Helper (standing task, followed topic), a Goal's cadence, or a file's studio. */
export const ACTIVITY_SOURCE_ICON: Record<ActivitySource, LucideIcon> = {
  turn: MessageCircle,
  background: Bot,
  housekeeping: NotebookPen,
  scheduled: CalendarClock,
  studio: Pencil,
};
