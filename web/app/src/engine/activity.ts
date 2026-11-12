import {
  getOmnigentActivity,
  listOmnigentActivities,
  mapActivity,
  streamOmnigentActivityChanges,
} from "@superchat/adapters/omnigent/browser";
import type { ActivityChanged } from "@superchat/contracts";
import { engineClient, museSessionId, NO_EMAIL } from "./client";
import { onSuperChat, os } from "./context";

/** The day boundaries the feed groups by are the person's own: this page's timezone. */
const timezone = () => Intl.DateTimeFormat().resolvedOptions().timeZone;

/** Tells the panel (and the chat's Helper rows) when to re-read the feed, relayed from the
 * engine's own session streams, so a Helper shows the moment it starts. The stream ends when the
 * engine's does; the web reconnects and re-reads. */
export async function* watchActivities(
  signal: AbortSignal | undefined,
): AsyncGenerator<ActivityChanged> {
  const superSessionId = await museSessionId();
  try {
    for await (const frame of streamOmnigentActivityChanges(
      engineClient(),
      NO_EMAIL,
      superSessionId,
      signal,
    )) {
      if (frame.type === "activities.changed") yield { type: "changed" };
      else if (frame.type === "session.heartbeat") yield { type: "heartbeat" };
    }
  } catch (error) {
    // A closed tab aborts the fetch; that is the normal end of a watch, not a failure.
    if (!signal?.aborted) throw error;
  }
}

export const activityRouter = {
  activities: {
    list: os.activities.list.handler(async ({ input }) => {
      const page = await onSuperChat(
        listOmnigentActivities(engineClient(), NO_EMAIL, await museSessionId(), {
          before: input.before,
          limit: input.limit,
          tz: timezone(),
        }),
      );
      return {
        activities: page.data.map((raw) => mapActivity(raw)),
        hasMore: Boolean(page.has_more),
      };
    }),
    get: os.activities.get.handler(async ({ input }) =>
      mapActivity(
        await onSuperChat(
          getOmnigentActivity(engineClient(), NO_EMAIL, await museSessionId(), input.activityId, {
            tz: timezone(),
          }),
        ),
      ),
    ),
    watch: os.activities.watch.handler(async function* ({ signal }) {
      yield* watchActivities(signal);
    }),
  },
};
