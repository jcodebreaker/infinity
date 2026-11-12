import {
  cn,
  type LiveMuseFaceProps,
  MUSE_FACE_CHEEK,
  MUSE_FACE_INK,
  MUSE_FACE_SHINE,
  MUSE_FACE_SPARK,
  MUSE_SPARK_PATH,
  MuseAvatar,
} from "@superchat/ui-web";
import { useEffect, useRef, useState } from "react";
import { supportsWebGL } from "../webgl";
import { bloopPalette } from "./expression";
import type { LiveBloopHandle } from "./live";

// The Muse's 3D face (the old design notes): a jelly drop whose expression follows the Muse state.
// It is supplied app-wide through `LiveMuseFaceProvider`, so every `BotAvatar face="muse"` uses
// it. The static SVG face is only a fallback, shown when WebGL is unavailable, fails to start, or
// the page already shows its share of live faces; it is never drawn first and then replaced. The
// Ask badge is always drawn. The shared renderer (live.ts) is warmed at app start (main.tsx).

/** Below this size the thought cloud, spinner and "…" bubble would be unreadable specks. */
const PROPS_MIN_SIZE = 32;
const FACE_COLORS = {
  ink: MUSE_FACE_INK,
  cheek: MUSE_FACE_CHEEK,
  shine: MUSE_FACE_SHINE,
  spark: MUSE_FACE_SPARK,
  sparkPath: MUSE_SPARK_PATH,
};

export function BloopAvatar({ color, size, state, waitingCount, className }: LiveMuseFaceProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const handleRef = useRef<LiveBloopHandle | null>(null);
  const latest = useRef({ color, state, size });
  latest.current = { color, state, size };
  // loading: the 3D face is on its way (nothing drawn yet); ready: showing; fallback: static face.
  const [mode, setMode] = useState<"loading" | "ready" | "fallback">(() =>
    supportsWebGL() ? "loading" : "fallback",
  );
  const withProps = size >= PROPS_MIN_SIZE;

  useEffect(() => {
    const canvas = canvasRef.current;
    const wrapper = wrapperRef.current;
    if (!canvas || !wrapper) return;
    let disposed = false;
    let acquiring = false;
    let observer: IntersectionObserver | null = null;

    // A face claims a live slot only once it is on screen, so a long list does not spend them all.
    function start() {
      if (acquiring || handleRef.current) return;
      acquiring = true;
      void import("./live").then(({ acquireLiveBloop }) =>
        acquireLiveBloop({
          canvas: canvas as HTMLCanvasElement,
          cssSize: latest.current.size,
          color: latest.current.color,
          state: latest.current.state,
          face: FACE_COLORS,
          withProps,
        }).then((handle) => {
          acquiring = false;
          if (!handle) {
            if (!disposed) setMode("fallback");
            return;
          }
          if (disposed) {
            handle.release();
            return;
          }
          handleRef.current = handle;
          setMode("ready");
        }),
      );
    }

    if (typeof IntersectionObserver === "undefined") {
      start();
    } else {
      observer = new IntersectionObserver((entries) => {
        const entry = entries[entries.length - 1];
        if (!entry) return;
        if (entry.isIntersecting) start();
        handleRef.current?.setVisible(entry.isIntersecting);
      });
      observer.observe(wrapper);
    }

    return () => {
      disposed = true;
      observer?.disconnect();
      handleRef.current?.release();
      handleRef.current = null;
      setMode((current) => (current === "fallback" ? current : "loading"));
    };
  }, [withProps]);

  useEffect(() => handleRef.current?.setState(state), [state]);
  useEffect(() => handleRef.current?.setColor(color), [color]);
  useEffect(() => handleRef.current?.resize(size), [size]);

  const scale = withProps ? 2.1 : 1.5;
  const canvasSize = Math.round(size * scale);
  return (
    <div
      ref={wrapperRef}
      data-bloop={mode}
      className={cn("relative inline-flex shrink-0 items-center justify-center", className)}
      style={{ width: size, height: size }}
    >
      <div
        aria-hidden="true"
        className={cn(
          "pointer-events-none absolute start-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 transition-opacity duration-300 motion-reduce:transition-none",
          mode === "ready" ? "opacity-100" : "opacity-0",
        )}
        style={{ width: canvasSize, height: canvasSize }}
      >
        <canvas ref={canvasRef} className="size-full" />
      </div>
      {mode === "loading" ? (
        // Until the 3D face is ready: a soft drop in its own tint, never the old flat face.
        <div
          aria-hidden="true"
          data-testid="bloop-placeholder"
          className="absolute start-1/2 top-[58%] -translate-x-1/2 -translate-y-1/2 rotate-[-45deg] animate-pulse motion-reduce:animate-none"
          style={{
            width: size * 0.5,
            height: size * 0.5,
            borderRadius: "0 50% 50% 50%",
            background: bloopPalette(color).top,
            opacity: 0.7,
          }}
        />
      ) : null}
      <MuseAvatar
        color={color}
        size={size}
        state={state}
        waitingCount={waitingCount}
        faceHidden={mode !== "fallback"}
      />
    </div>
  );
}
