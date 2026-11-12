import { ORPCError } from "@orpc/client";
import { useSyncExternalStore } from "react";
import { familyActivityChanges } from "../../lib/family-stream";
import { rpc } from "../../lib/rpc";
import type { ActivityWire } from "./ActivityRunDialog";
import { type ActivitiesState, ActivityFeed } from "./activityFeed";

export type { ActivitiesState } from "./activityFeed";

// `NOT_IMPLEMENTED`: the backend doesn't serve `activities.*` in this environment.
// `NOT_FOUND`: this Muse has no Conversation yet (a brand new Muse) — both are the
// calm "nothing yet" state, never "Could not load…" (docs/helpers.md: the
// Activity panel's own empty state already says this plainly).
const isUnavailable = (error: unknown) =>
  error instanceof ORPCError && (error.code === "NOT_IMPLEMENTED" || error.code === "NOT_FOUND");

/** The Activity run page's live wire: full detail and a Helper's messages. */
export const LIVE_ACTIVITY_WIRE: ActivityWire = {
  get: (input) => rpc.activities.get(input),
  helperMessages: (input) => rpc.chats.transcript(input),
};

const feeds = new Map<string, ActivityFeed>();

/** The one live feed for a Muse, shared by the Activity panel and the chat's Helper rows. */
export function activityFeedFor(botId: string): ActivityFeed {
  let feed = feeds.get(botId);
  if (!feed) {
    feed = new ActivityFeed(botId, {
      list: (input) => rpc.activities.list(input),
      // Off the shared family stream: one long-lived connection per tab, not two.
      watch: (input, signal) => familyActivityChanges(input, signal),
      isUnavailable,
    });
    feeds.set(botId, feed);
  }
  return feed;
}

/**
 * The Activity feed's data (docs/helpers.md "The activity panel"): live while mounted
 * (see ActivityFeed — event-driven, polled only as a safety net, live even while the tab is hidden).
 */
export function useActivities(botId: string): {
  state: ActivitiesState;
  loadEarlier: () => Promise<void>;
  loadingEarlier: boolean;
} {
  const feed = activityFeedFor(botId);
  const { state, loadingEarlier } = useSyncExternalStore(feed.subscribe, feed.getSnapshot);
  return { state, loadEarlier: feed.loadEarlier, loadingEarlier };
}
