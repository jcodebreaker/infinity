import {
  getOmnigentProactivity,
  type OmnigentProactivity,
  putOmnigentProactivity,
} from "@superchat/adapters/omnigent/browser";
import type { MuseSettings } from "@superchat/contracts";
import { engineClient, NO_EMAIL } from "./client";
import { os } from "./context";

// The Muse's settings are the person's engine preferences (`/v1/me/proactivity`, ADR 0009): the
// engine gates every proactive run with them, so it holds the only copy.

function fromEngine(prefs: OmnigentProactivity): MuseSettings {
  return {
    proactivity: prefs.proactivity,
    quietHours:
      prefs.quiet_start && prefs.quiet_end ? `${prefs.quiet_start}-${prefs.quiet_end}` : null,
  };
}

function toEngine(patch: Partial<MuseSettings>): Parameters<typeof putOmnigentProactivity>[2] {
  const body: Parameters<typeof putOmnigentProactivity>[2] = {};
  if (patch.proactivity !== undefined) body.proactivity = patch.proactivity;
  if (patch.quietHours !== undefined) {
    const [start = null, end = null] = patch.quietHours?.split("-") ?? [];
    body.quiet_start = start;
    body.quiet_end = end;
  }
  return body;
}

export const museRouter = {
  muse: {
    settings: os.muse.settings.handler(async () =>
      fromEngine(await getOmnigentProactivity(engineClient(), NO_EMAIL)),
    ),
    updateSettings: os.muse.updateSettings.handler(async ({ input }) => {
      const { botId: _botId, ...patch } = input;
      return fromEngine(await putOmnigentProactivity(engineClient(), NO_EMAIL, toEngine(patch)));
    }),
  },
};
