import type { MuseState } from "@superchat/contracts";
import type * as ThreeNamespace from "three";
import { readCssColor, supportsWebGL } from "../webgl";
import { type BloopPropTextures, type BloopTheme, buildPropTextures } from "./props";
import { type BloopFaceColors, type BloopScene, createBloopScene } from "./scene";

// One WebGL renderer for every live Muse face on the page. Each avatar owns a 2D canvas; every
// frame the shared renderer draws that avatar's scene into a corner of its own buffer and the
// result is copied across. A page can show dozens of faces without dozens of WebGL contexts
// (browsers cap those at about sixteen), and the renderer is released when the last one goes.

const REDUCED_MOTION_QUERY = "(prefers-reduced-motion: reduce)";
const TARGET_FPS = 30;
const FRAME_BUDGET_MS = 1000 / TARGET_FPS;
/** Beyond this many live faces the rest stay as the static SVG. */
const MAX_LIVE = 12;
const MAX_PIXELS = 640;
/** How much larger than its layout box the canvas is: props need room, small faces do not. */
const SCALE_WITH_PROPS = 2.1;
const SCALE_TIGHT = 1.5;

type Three = typeof ThreeNamespace;

interface Shared {
  THREE: Three;
  renderer: ThreeNamespace.WebGLRenderer;
  props: BloopPropTextures;
  bufferSize: number;
  themeObserver: MutationObserver;
}

interface Slot {
  scene: BloopScene;
  canvas: HTMLCanvasElement;
  context: CanvasRenderingContext2D;
  cssSize: number;
  withProps: boolean;
  visible: boolean;
  pixels: number;
}

export interface LiveBloopInit {
  canvas: HTMLCanvasElement;
  cssSize: number;
  color: string;
  state: MuseState;
  face: BloopFaceColors;
  /** Draw the thought cloud, spinner and "…" bubble. Off for small faces. */
  withProps: boolean;
}

export interface LiveBloopHandle {
  setState(state: MuseState): void;
  setColor(color: string): void;
  setVisible(visible: boolean): void;
  resize(cssSize: number): void;
  release(): void;
}

/** The canvas box is larger than the avatar's layout box by this factor. */
export function bloopCanvasScale(withProps: boolean): number {
  return withProps ? SCALE_WITH_PROPS : SCALE_TIGHT;
}

const slots = new Set<Slot>();
let reserved = 0;
let shared: Shared | null = null;
let sharedLoading: Promise<Shared | null> | null = null;
let raf = 0;
let lastFrame = 0;

const reduceMotion = () => window.matchMedia(REDUCED_MOTION_QUERY).matches;

function toCss([r, g, b]: [number, number, number]): string {
  return `rgb(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)})`;
}

function readTheme(): BloopTheme {
  return {
    card: toCss(readCssColor("--card", [1, 1, 1])),
    border: toCss(readCssColor("--border", [0.85, 0.85, 0.87])),
    foreground: toCss(readCssColor("--foreground", [0.08, 0.09, 0.1])),
  };
}

function onPointerMove(event: PointerEvent) {
  const x = (event.clientX / window.innerWidth) * 2 - 1;
  const y = 1 - (event.clientY / window.innerHeight) * 2;
  for (const slot of slots) slot.scene.setPointer(x, y);
}

async function loadShared(): Promise<Shared | null> {
  try {
    const THREE = await import("three");
    const renderer = new THREE.WebGLRenderer({
      antialias: true,
      alpha: true,
      powerPreference: "low-power",
    });
    renderer.setPixelRatio(1);
    renderer.setClearColor(0x000000, 0);
    const props = buildPropTextures(THREE, readTheme());
    const themeObserver = new MutationObserver(() => {
      if (!shared) return;
      shared.props.dispose();
      shared.props = buildPropTextures(THREE, readTheme());
      for (const slot of slots) {
        if (slot.withProps) slot.scene.setProps(shared.props);
        redrawStill(slot);
      }
    });
    themeObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    renderer.domElement.addEventListener("webglcontextlost", () => {
      // A lost context cannot be revived here; the static faces underneath take over.
      for (const slot of slots) slot.canvas.style.opacity = "0";
    });
    if (!reduceMotion()) window.addEventListener("pointermove", onPointerMove, { passive: true });
    return { THREE, renderer, props, bufferSize: 0, themeObserver };
  } catch {
    return null;
  }
}

function drawSlot(slot: Slot, nowMs: number) {
  if (!shared) return;
  const { renderer } = shared;
  const needed = Math.max(...Array.from(slots, (s) => s.pixels));
  if (needed > shared.bufferSize) {
    shared.bufferSize = needed;
    renderer.setSize(needed, needed, false);
  }
  const px = slot.pixels;
  renderer.setScissorTest(true);
  renderer.setViewport(0, 0, px, px);
  renderer.setScissor(0, 0, px, px);
  slot.scene.update(nowMs);
  slot.scene.render(renderer);
  slot.context.clearRect(0, 0, px, px);
  slot.context.drawImage(renderer.domElement, 0, shared.bufferSize - px, px, px, 0, 0, px, px);
}

/** Reduced motion: one still frame a moment into the state, redrawn only when something changes. */
function redrawStill(slot: Slot) {
  if (reduceMotion()) drawSlot(slot, performance.now() + 1500);
}

function loop(time: number) {
  raf = window.requestAnimationFrame(loop);
  if (time - lastFrame < FRAME_BUDGET_MS) return;
  lastFrame = time;
  for (const slot of slots) if (slot.visible) drawSlot(slot, time);
}

function startLoop() {
  if (raf || reduceMotion()) return;
  raf = window.requestAnimationFrame(loop);
}

function stopLoop() {
  if (!raf) return;
  window.cancelAnimationFrame(raf);
  raf = 0;
}

function onVisibilityChange() {
  if (document.hidden) stopLoop();
  else if (slots.size > 0) startLoop();
}

function teardownShared() {
  stopLoop();
  document.removeEventListener("visibilitychange", onVisibilityChange);
  window.removeEventListener("pointermove", onPointerMove);
  if (shared) {
    shared.themeObserver.disconnect();
    shared.props.dispose();
    shared.renderer.dispose();
    shared.renderer.forceContextLoss();
  }
  shared = null;
  sharedLoading = null;
}

function pixelsFor(cssSize: number, withProps: boolean): number {
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  return Math.min(
    MAX_PIXELS,
    Math.max(16, Math.round(cssSize * bloopCanvasScale(withProps) * ratio)),
  );
}

/**
 * Claims a live face drawn into `init.canvas`, or returns null (no WebGL, the page already shows
 * as many live faces as it should, or the renderer could not start) so the caller keeps the
 * static one.
 */
export async function acquireLiveBloop(init: LiveBloopInit): Promise<LiveBloopHandle | null> {
  if (!supportsWebGL() || reserved >= MAX_LIVE) return null;
  const context = init.canvas.getContext("2d");
  if (!context) return null;
  reserved += 1;

  sharedLoading ??= loadShared();
  const loaded = await sharedLoading;
  if (!loaded) {
    reserved -= 1;
    sharedLoading = null;
    return null;
  }
  if (!shared) {
    shared = loaded;
    document.addEventListener("visibilitychange", onVisibilityChange);
  }

  const scene = createBloopScene(loaded.THREE, {
    color: init.color,
    state: init.state,
    amp: reduceMotion() ? 0.35 : 1,
    face: init.face,
    props: init.withProps ? loaded.props : null,
  });
  const slot: Slot = {
    scene,
    canvas: init.canvas,
    context,
    cssSize: init.cssSize,
    withProps: init.withProps,
    visible: true,
    pixels: pixelsFor(init.cssSize, init.withProps),
  };
  slot.canvas.width = slot.pixels;
  slot.canvas.height = slot.pixels;
  slots.add(slot);
  if (reduceMotion()) redrawStill(slot);
  else startLoop();

  return {
    setState(state) {
      slot.scene.setState(state);
      redrawStill(slot);
    },
    setColor(color) {
      slot.scene.setColor(color);
      redrawStill(slot);
    },
    setVisible(visible) {
      slot.visible = visible;
    },
    resize(cssSize) {
      slot.cssSize = cssSize;
      slot.pixels = pixelsFor(cssSize, slot.withProps);
      slot.canvas.width = slot.pixels;
      slot.canvas.height = slot.pixels;
      redrawStill(slot);
    },
    release() {
      if (!slots.delete(slot)) return;
      slot.scene.dispose();
      reserved -= 1;
      if (slots.size === 0) teardownShared();
    },
  };
}
