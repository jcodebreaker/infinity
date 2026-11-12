import {
  getOmnigentMuse,
  type OmnigentClientConfig,
  omnigentClientFor,
} from "@superchat/adapters/omnigent/browser";

/**
 * The engine as this page reaches it: the same origin, with no identity of its own. Who the person
 * is comes from outside the browser (the sign-in broker in front of the engine, or the engine's
 * single-user local mode), so no email, tenant or proxy secret is ever sent from here.
 */
export function engineClient(): OmnigentClientConfig {
  return omnigentClientFor({ baseUrl: window.location.origin, proxySecret: "", secrets: [] }, "");
}

/** The engine's calls take the caller's email for its identity header; the engine already knows
 * the person, so there is none to send. */
export const NO_EMAIL = "";

let muse: Promise<string> | undefined;

/**
 * The person's Muse session (`GET /v1/me/muse`), resolved once. Its id is also the `botId` the
 * rest of the app passes around: there is one Muse per person, so the session identifies it.
 */
export function museSessionId(): Promise<string> {
  muse ??= getOmnigentMuse(engineClient(), NO_EMAIL)
    .then((found) => found.session_id)
    .catch((error: unknown) => {
      muse = undefined;
      throw error;
    });
  return muse;
}

/** Forgets the resolved Muse, for when the engine answers that the session is gone. */
export function forgetMuseSession(): void {
  muse = undefined;
}
