let webglSupport: boolean | null = null;

/** Whether this browser can create a WebGL context. Probed once: every probe makes a context. */
export function supportsWebGL(): boolean {
  if (webglSupport !== null) return webglSupport;
  if (typeof window === "undefined" || typeof window.WebGLRenderingContext === "undefined") {
    return false;
  }
  try {
    const canvas = document.createElement("canvas");
    webglSupport = Boolean(canvas.getContext("webgl2") || canvas.getContext("webgl"));
  } catch {
    webglSupport = false;
  }
  return webglSupport;
}

export function parseColor(
  raw: string,
  fallback: [number, number, number],
): [number, number, number] {
  const value = raw.trim();
  const hex = /^#([0-9a-f]{6})$/i.exec(value);
  const hexDigits = hex?.[1];
  if (hexDigits) {
    const int = Number.parseInt(hexDigits, 16);
    return [((int >> 16) & 255) / 255, ((int >> 8) & 255) / 255, (int & 255) / 255];
  }
  const rgb = /^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)/i.exec(value);
  if (rgb) {
    return [Number(rgb[1]) / 255, Number(rgb[2]) / 255, Number(rgb[3]) / 255];
  }
  return fallback;
}

/** Reads a semantic CSS color variable (e.g. `--border`) as 0..1 RGB, or `fallback` when unset. */
export function readCssColor(
  name: string,
  fallback: [number, number, number],
): [number, number, number] {
  return parseColor(getComputedStyle(document.documentElement).getPropertyValue(name), fallback);
}
