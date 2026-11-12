import { ORPCError } from "@orpc/server";
import {
  getOmnigentIdentity,
  getOmnigentProactivity,
  getOmnigentSession,
  putOmnigentTimezone,
} from "@superchat/adapters/omnigent/browser";
import {
  type AvatarStyle,
  type Bot,
  DEFAULT_MUSE_COLOR,
  DEFAULT_MUSE_NAME,
  type Me,
  museBotProfile,
} from "@superchat/contracts";
import { engineClient, museSessionId, NO_EMAIL } from "./client";
import { os } from "./context";
import { modelDefaults, modelsReady } from "./models";

const AVATAR_STYLE_KEY = "superchat:avatar-style";

function storedAvatarStyle(): AvatarStyle {
  try {
    return window.localStorage.getItem(AVATAR_STYLE_KEY) === "organic" ? "organic" : "robot";
  } catch {
    return "robot";
  }
}

function storeAvatarStyle(style: AvatarStyle): void {
  try {
    window.localStorage.setItem(AVATAR_STYLE_KEY, style);
  } catch {
    // The preference only lasts for this page load when storage is unavailable.
  }
}

/** A name to greet the person by: the part of their identity before the `@`. */
function displayName(userId: string): string {
  return userId.split("@")[0] ?? userId;
}

async function currentMe(): Promise<Me> {
  const identity = await getOmnigentIdentity(engineClient(), NO_EMAIL);
  if (!identity) throw new ORPCError("UNAUTHORIZED");
  const [ready, defaults, proactivity] = await Promise.all([
    modelsReady(),
    modelDefaults(),
    getOmnigentProactivity(engineClient(), NO_EMAIL),
  ]);
  return {
    userId: identity.userId,
    name: displayName(identity.userId),
    isAdmin: identity.isAdmin,
    needsModel: !ready,
    defaultProvider: defaults.provider,
    defaultModel: defaults.model,
    avatarStyle: storedAvatarStyle(),
    timezone: proactivity.timezone,
  };
}

/** The person's one Muse, drawn from its engine session. */
async function museBot(): Promise<Bot> {
  const [me, sessionId] = await Promise.all([currentMe(), museSessionId()]);
  const session = await getOmnigentSession(engineClient(), NO_EMAIL, sessionId);
  const profile = museBotProfile(DEFAULT_MUSE_NAME, me.name);
  const stamp = (value: unknown) =>
    typeof value === "number" ? new Date(value * 1000).toISOString() : new Date().toISOString();
  return {
    id: sessionId,
    name: DEFAULT_MUSE_NAME,
    title: profile.title,
    description: profile.description,
    color: DEFAULT_MUSE_COLOR,
    unread: false,
    preview: "",
    status: session.status,
    updatedAt: stamp(session.updated_at),
    createdAt: stamp(session.created_at),
  };
}

export const accountRouter = {
  me: os.me.handler(() => currentMe()),
  preferences: {
    update: os.preferences.update.handler(async ({ input }) => {
      if (input.avatarStyle) storeAvatarStyle(input.avatarStyle);
      if (input.timezone) await putOmnigentTimezone(engineClient(), NO_EMAIL, input.timezone);
      return currentMe();
    }),
  },
  bots: {
    list: os.bots.list.handler(async () => [await museBot()]),
  },
};
