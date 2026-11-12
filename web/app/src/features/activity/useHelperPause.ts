import type { Activity } from "@superchat/contracts";
import { useCallback, useEffect, useRef, useState } from "react";
import { rpc } from "../../lib/rpc";

// Pause (ADR 0027): holds a reply's Helpers at their next tool boundary and lets them go on. The
// engine flags a held Helper on its Activity (`paused`, from the feed the strip already reads);
// while the person's press is on its way (until the call returns, at most 10 s) the press stands
// in.

/** The longest a press stands in for the engine's answer. */
export const PRESS_STANDS_MS = 10_000;

export type PauseOverride = "paused" | "resumed" | null;

/** Whether the strip reads Paused: the press on its way, else what the engine says. */
export function pausedReading(override: PauseOverride, held: boolean): boolean {
  if (override === "paused") return true;
  if (override === "resumed") return false;
  return held;
}

/** Pause and Resume for the Helpers a reply has running (their Activities in the feed). */
export function useHelperPause(
  botId: string,
  running: readonly Pick<Activity, "chatId" | "paused">[],
): { paused: boolean; pause: () => Promise<void>; resume: () => Promise<void> } {
  const enabled = running.length > 0;
  const held = running.some((activity) => activity.paused === true);
  const [override, setOverride] = useState<PauseOverride>(null);
  const press = useRef(0);
  const key = running.map((activity) => activity.chatId).join(" ");
  /** Stands in while `call` is on its way, at most `PRESS_STANDS_MS`. */
  const pressed = useCallback(async (reading: PauseOverride, call: () => Promise<unknown>) => {
    const mine = ++press.current;
    const end = () => {
      if (press.current === mine) setOverride(null);
    };
    setOverride(reading);
    const timer = setTimeout(end, PRESS_STANDS_MS);
    try {
      await call();
    } catch {
      // Nothing changed: the engine's reading stands.
    } finally {
      clearTimeout(timer);
      end();
    }
  }, []);
  useEffect(() => {
    if (!enabled) setOverride(null);
  }, [enabled]);
  const pause = useCallback(
    () => pressed("paused", () => rpc.chats.pauseHelpers({ botId, helperIds: key.split(" ") })),
    [botId, key, pressed],
  );
  const resume = useCallback(
    () => pressed("resumed", () => rpc.chats.resumeHelpers({ botId, helperIds: key.split(" ") })),
    [botId, key, pressed],
  );
  return { paused: enabled && pausedReading(override, held), pause, resume };
}
