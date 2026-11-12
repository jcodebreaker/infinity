import {
  drawnOrbEnergy,
  type OrbState,
  orbEnergyTarget,
  orbPixelSize,
  stepOrbEnergy,
} from "./orbState";
import { ORB_FRAGMENT_SHADER, ORB_VERTEX_SHADER } from "./shader";

/**
 * One shared WebGL renderer for every Superchat orb on the page.
 *
 * A page may show many orbs (the sidebar, the inspector, one per working row), and browsers
 * cap live WebGL contexts at ~16. So there is exactly one hidden WebGL canvas: each frame it
 * renders every visible orb in turn at that orb's own pixel size and copies the result into
 * the orb's plain 2D canvas. One requestAnimationFrame loop drives them all; it stops while
 * the tab is hidden, while no orb is on screen (IntersectionObserver), and entirely under
 * reduced motion, where each orb gets one still frame. When the last orb unmounts the GL
 * context is released, so nothing lingers.
 */

type Orb = {
  canvas: HTMLCanvasElement;
  ctx: CanvasRenderingContext2D;
  size: number;
  px: number;
  state: OrbState;
  energy: number;
  time: number;
  seed: number;
  visible: boolean;
  onLost: () => void;
};

type Gl = {
  canvas: HTMLCanvasElement;
  gl: WebGLRenderingContext;
  u: {
    r: WebGLUniformLocation | null;
    t: WebGLUniformLocation | null;
    e: WebGLUniformLocation | null;
    l: WebGLUniformLocation | null;
  };
};

const orbs = new Set<Orb>();
let shared: Gl | null = null;
let failed = false;
let raf = 0;
let last = 0;
let observer: IntersectionObserver | null = null;
let themeObserver: MutationObserver | null = null;
const byCanvas = new WeakMap<Element, Orb>();

function reducedMotion(): boolean {
  return typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-reduced-motion: reduce)").matches
    : false;
}

function isLight(): boolean {
  return document.documentElement.dataset.theme !== "dark";
}

function compile(gl: WebGLRenderingContext): WebGLProgram | null {
  const shader = (type: number, source: string) => {
    const s = gl.createShader(type);
    if (!s) return null;
    gl.shaderSource(s, source);
    gl.compileShader(s);
    return gl.getShaderParameter(s, gl.COMPILE_STATUS) ? s : null;
  };
  const vs = shader(gl.VERTEX_SHADER, ORB_VERTEX_SHADER);
  const fs = shader(gl.FRAGMENT_SHADER, ORB_FRAGMENT_SHADER);
  const program = gl.createProgram();
  if (!vs || !fs || !program) return null;
  gl.attachShader(program, vs);
  gl.attachShader(program, fs);
  gl.linkProgram(program);
  return gl.getProgramParameter(program, gl.LINK_STATUS) ? program : null;
}

function ensureGl(): Gl | null {
  if (shared) return shared;
  if (failed || typeof document === "undefined") return null;
  try {
    const canvas = document.createElement("canvas");
    canvas.width = 1;
    canvas.height = 1;
    const gl = canvas.getContext("webgl", {
      alpha: true,
      antialias: true,
      premultipliedAlpha: false,
      preserveDrawingBuffer: true,
    });
    const program = gl ? compile(gl) : null;
    if (!gl || !program) {
      failed = true;
      return null;
    }
    // biome-ignore lint/correctness/useHookAtTopLevel: WebGL's useProgram, not a React hook.
    gl.useProgram(program);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    const position = gl.getAttribLocation(program, "p");
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    canvas.addEventListener("webglcontextlost", () => {
      // A context we released on purpose (the last orb unmounted) is not a failure.
      if (shared?.canvas !== canvas) return;
      failed = true;
      shared = null;
      stop();
      for (const orb of [...orbs]) orb.onLost();
    });
    shared = {
      canvas,
      gl,
      u: {
        r: gl.getUniformLocation(program, "r"),
        t: gl.getUniformLocation(program, "t"),
        e: gl.getUniformLocation(program, "energy"),
        l: gl.getUniformLocation(program, "light"),
      },
    };
    return shared;
  } catch {
    failed = true;
    return null;
  }
}

function release() {
  stop();
  observer?.disconnect();
  observer = null;
  themeObserver?.disconnect();
  themeObserver = null;
  document.removeEventListener("visibilitychange", onVisibility);
  if (shared) {
    shared.gl.getExtension("WEBGL_lose_context")?.loseContext();
    shared.canvas.width = 1;
    shared.canvas.height = 1;
    shared = null;
  }
}

function draw(orb: Orb, light: number) {
  const g = shared;
  if (!g) return;
  const { gl, canvas } = g;
  const px = orb.px;
  if (canvas.width < px || canvas.height < px) {
    canvas.width = Math.max(canvas.width, px);
    canvas.height = Math.max(canvas.height, px);
  }
  gl.viewport(0, 0, px, px);
  gl.clearColor(0, 0, 0, 0);
  gl.clear(gl.COLOR_BUFFER_BIT);
  gl.uniform2f(g.u.r, px, px);
  gl.uniform1f(g.u.t, orb.time + orb.seed);
  gl.uniform1f(g.u.e, drawnOrbEnergy(orb.energy, orb.size));
  gl.uniform1f(g.u.l, light);
  gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  // GL's origin is bottom-left, so this orb's square sits at the bottom of the shared canvas.
  orb.ctx.clearRect(0, 0, px, px);
  orb.ctx.drawImage(canvas, 0, canvas.height - px, px, px, 0, 0, px, px);
}

function still(orb: Orb) {
  orb.energy = orbEnergyTarget(orb.state);
  draw(orb, isLight() ? 1 : 0);
}

function frame(now: number) {
  raf = 0;
  const dt = Math.min(0.05, (now - last) / 1000);
  last = now;
  const light = isLight() ? 1 : 0;
  let any = false;
  for (const orb of orbs) {
    if (!orb.visible) continue;
    any = true;
    orb.energy = stepOrbEnergy(orb.energy, orb.state, dt);
    orb.time += dt * (1 + 1.6 * orb.energy);
    draw(orb, light);
  }
  if (any) raf = requestAnimationFrame(frame);
}

function start() {
  if (raf || !shared || reducedMotion() || document.visibilityState === "hidden") return;
  if (![...orbs].some((orb) => orb.visible)) return;
  last = performance.now();
  raf = requestAnimationFrame(frame);
}

function stop() {
  if (raf) cancelAnimationFrame(raf);
  raf = 0;
}

function onVisibility() {
  if (document.visibilityState === "hidden") stop();
  else start();
}

function watch() {
  if (!observer && typeof IntersectionObserver !== "undefined") {
    observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        const orb = byCanvas.get(entry.target);
        if (!orb) continue;
        orb.visible = entry.isIntersecting;
        if (orb.visible && reducedMotion()) still(orb);
      }
      start();
    });
  }
  if (!themeObserver && typeof MutationObserver !== "undefined") {
    // A theme switch repaints still orbs; animated ones pick it up on their next frame.
    themeObserver = new MutationObserver(() => {
      if (raf) return;
      for (const orb of orbs) if (orb.visible) still(orb);
    });
    themeObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
  }
  if (orbs.size === 1) document.addEventListener("visibilitychange", onVisibility);
}

export type OrbHandle = {
  setState: (state: OrbState) => void;
  dispose: () => void;
};

/**
 * Draws Superchat's orb into `canvas` (a 2D canvas the caller owns) at `size` CSS pixels.
 * Returns null when WebGL is unavailable, so the caller shows its CSS fallback instead.
 */
export function registerOrb(
  canvas: HTMLCanvasElement,
  size: number,
  state: OrbState,
  onLost: () => void,
): OrbHandle | null {
  if (!ensureGl()) return null;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  const px = orbPixelSize(size, window.devicePixelRatio);
  canvas.width = px;
  canvas.height = px;
  const orb: Orb = {
    canvas,
    ctx,
    size,
    px,
    state,
    energy: orbEnergyTarget(state),
    time: 3,
    seed: Math.random() * 10,
    visible: typeof IntersectionObserver === "undefined",
    onLost,
  };
  orbs.add(orb);
  byCanvas.set(canvas, orb);
  watch();
  observer?.observe(canvas);
  still(orb);
  start();
  return {
    setState(next) {
      if (orb.state === next) return;
      orb.state = next;
      if (reducedMotion() || !raf) still(orb);
    },
    dispose() {
      observer?.unobserve(canvas);
      orbs.delete(orb);
      byCanvas.delete(canvas);
      if (orbs.size === 0) release();
    },
  };
}

/** Test seam: forget a failed probe so a test can try again with a different environment. */
export function resetOrbRendererForTests() {
  release();
  orbs.clear();
  failed = false;
}
