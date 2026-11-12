import type { Bot, Me } from "@superchat/contracts";
import { isBootstrapRoute } from "./bootstrap-target";
import { markOnce } from "./performance";
import { rpc } from "./rpc";

export type AppBootstrap = { me: Me; bots: Bot[] };

let mePromise: Promise<Me> | null = null;

/** The signed-in person, asked of the engine once and shared by the app gate and the Shell. */
export function loadMe(): Promise<Me> {
  if (!mePromise) {
    const request = rpc.me();
    mePromise = request;
    // A failed lookup must not stick: the next caller asks again.
    request.catch(() => {
      if (mePromise === request) mePromise = null;
    });
  }
  return mePromise;
}

/** Forget the shared lookup so the next `loadMe` asks the engine again. */
export function resetMe() {
  mePromise = null;
  primedBootstrap = null;
}

function requestBootstrap(): Promise<AppBootstrap> {
  markOnce("rk:renderer:bootstrap-request-start");
  return Promise.all([loadMe(), rpc.bots.list()]).then(([me, bots]) => {
    markOnce("rk:renderer:bootstrap-response");
    return { me, bots };
  });
}

let primedBootstrap: Promise<AppBootstrap> | null = null;

if (isBootstrapRoute(window.location.pathname)) {
  primedBootstrap = requestBootstrap();
  // The gate may show an error before the Shell consumes this speculative request.
  // Register a rejection handler immediately while preserving rejection for a consumer.
  void primedBootstrap.catch(() => undefined);
}

export function takeInitialBootstrap(): Promise<AppBootstrap> {
  const primed = primedBootstrap;
  primedBootstrap = null;
  return primed ?? requestBootstrap();
}
