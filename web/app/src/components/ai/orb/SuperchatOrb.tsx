import { cn } from "@superchat/ui-web";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { supportsWebGL } from "../webgl";
import type { OrbState } from "./orbState";
import { useSuperchatPresence } from "./presence";
import { type OrbHandle, registerOrb } from "./renderer";

/**
 * Superchat's presence: a live shader orb (the old design notes "Orb"). Shader inspired by
 * "Shader Reminder" by Daniela Muntyan (CC BY 4.0); original implementation.
 *
 * Superchat has one orb on screen (placement.tsx): the start page, the sidebar header, or the toolbar.
 * `state` defaults to the shell's live presence, so every orb brightens while Superchat works.
 * All orbs share one WebGL renderer (renderer.ts). Without WebGL, or once the context is
 * lost, the orb is a still radial gradient (`.superchat-orb` in styles.css) of the same size, so
 * swapping never shifts layout.
 */
export function SuperchatOrb({
  size = 34,
  state,
  className,
  flies = true,
}: {
  size?: number;
  state?: OrbState;
  className?: string;
  /** The one placed orb carries the shared view-transition name; decorative copies (settings, mentions, cards) pass false. */
  flies?: boolean;
}) {
  const presence = useSuperchatPresence();
  const current = state ?? presence;
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const handle = useRef<OrbHandle | null>(null);
  const [fallback, setFallback] = useState(() => !supportsWebGL());
  const stateRef = useRef(current);
  stateRef.current = current;

  // A layout effect, so the first frame is drawn before paint: the orb never flashes empty,
  // including in the new state a View Transition captures when it lands in its home.
  useLayoutEffect(() => {
    const canvas = canvasRef.current;
    if (fallback || !canvas) return;
    const orb = registerOrb(canvas, size, stateRef.current, () => setFallback(true));
    if (!orb) {
      setFallback(true);
      return;
    }
    handle.current = orb;
    return () => {
      orb.dispose();
      handle.current = null;
    };
  }, [fallback, size]);

  useEffect(() => {
    handle.current?.setState(current);
  }, [current]);

  return (
    <span
      aria-hidden="true"
      data-testid="superchat-orb"
      data-superchat-orb=""
      data-orb-state={current}
      data-orb-fallback={fallback || undefined}
      className={cn("superchat-orb relative inline-block shrink-0 rounded-full", className)}
      // One orb is ever mounted (placement.tsx), so the shared name is unique: the View
      // Transition flies it from the start page to its home (flight.ts).
      style={{ width: size, height: size, viewTransitionName: flies ? "superchat-orb" : undefined }}
    >
      {fallback ? null : <canvas ref={canvasRef} className="block size-full rounded-full" />}
    </span>
  );
}
