import type { Bot, Me } from "@superchat/contracts";
import { useCallback, useEffect, useRef, useState } from "react";
import type { NavigateFunction } from "react-router-dom";
import { takeInitialBootstrap } from "../../../lib/bootstrap";
import { localTimezone } from "../../../lib/local-timezone";
import { markOnce } from "../../../lib/performance";
import { rpc } from "../../../lib/rpc";

/** Where the person lands: their Muse. */
function museRoute(bots: readonly Pick<Bot, "id">[]): string {
  return bots[0] ? `/app/${bots[0].id}` : "/app";
}

/** The person and their one Muse: bootstrap and background refresh. */
export function useBotRoster({
  botId,
  navigate,
}: {
  botId: string | undefined;
  navigate: NavigateFunction;
}) {
  const [bots, setBots] = useState<Bot[]>([]);
  const botsRef = useRef(bots);
  botsRef.current = bots;
  const botsRefreshEpoch = useRef(0);
  const botsRefreshApplied = useRef(0);
  const botsRefreshInFlight = useRef(0);
  const [initialBotsLoaded, setInitialBotsLoaded] = useState(false);
  const [bootstrapMe, setBootstrapMe] = useState<Me | null>();
  // Quiet hours and the daily topic digest run in the person's zone; keep it in step with
  // this browser (and a trip or DST-free move) without asking.
  const savedTimezone = bootstrapMe?.timezone;
  useEffect(() => {
    if (!savedTimezone) return;
    const timezone = localTimezone();
    if (timezone !== savedTimezone) {
      void rpc.preferences.update({ timezone }).catch(() => undefined);
    }
  }, [savedTimezone]);
  const routeBotId = useRef<string | undefined>(botId);
  routeBotId.current = botId;

  const refreshBots = useCallback(async () => {
    markOnce("rk:renderer:bots-request-start");
    const request = ++botsRefreshEpoch.current;
    botsRefreshInFlight.current += 1;
    try {
      const list = await rpc.bots.list();
      markOnce("rk:renderer:bots-response");
      // Only the latest response applies.
      if (request !== botsRefreshEpoch.current) return;
      setBots(list);
      setInitialBotsLoaded(true);
      botsRefreshApplied.current = request;
      const currentBotId = routeBotId.current;
      if (!currentBotId || !list.some((bot) => bot.id === currentBotId)) {
        navigate(museRoute(list), { replace: true });
      }
    } finally {
      botsRefreshInFlight.current -= 1;
    }
  }, [navigate]);

  useEffect(() => {
    let cancelled = false;
    const appliedAtStart = botsRefreshApplied.current;
    void takeInitialBootstrap()
      .then((bootstrap) => {
        if (cancelled) return;
        setBootstrapMe(bootstrap.me);
        // Skip list/route writes only if a later refreshBots() successfully
        // committed (failed refreshes bump epoch but not botsRefreshApplied).
        if (appliedAtStart !== botsRefreshApplied.current) return;
        setBots(bootstrap.bots);
        setInitialBotsLoaded(true);
        markOnce("rk:renderer:bots-response");
        const museId = bootstrap.bots[0]?.id;
        if (museId && museId !== botId) navigate(`/app/${museId}`, { replace: true });
      })
      .catch(() => {
        if (cancelled) return;
        setBootstrapMe(null);
        void refreshBots();
      });
    let refreshTimer: number | undefined;
    const refreshVisibleBots = () => {
      if (document.visibilityState !== "visible") return;
      window.clearTimeout(refreshTimer);
      refreshTimer = window.setTimeout(() => void refreshBots().catch(() => undefined), 50);
    };
    window.addEventListener("focus", refreshVisibleBots);
    document.addEventListener("visibilitychange", refreshVisibleBots);
    // Poll skips while a refresh is in flight; focus/visibility and event-driven
    // callers still bump botsRefreshEpoch so only the latest response applies.
    const poll = window.setInterval(() => {
      if (botsRefreshInFlight.current > 0) return;
      refreshVisibleBots();
      // Live events and focus refresh the Muse right away; this only catches drift.
    }, 10_000);
    return () => {
      cancelled = true;
      window.clearTimeout(refreshTimer);
      window.clearInterval(poll);
      window.removeEventListener("focus", refreshVisibleBots);
      document.removeEventListener("visibilitychange", refreshVisibleBots);
    };
  }, []);
  return {
    bots,
    botsRef,
    initialBotsLoaded,
    bootstrapMe,
    setBootstrapMe,
    refreshBots,
  };
}
