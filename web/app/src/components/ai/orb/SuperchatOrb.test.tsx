// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("@superchat/ui-web", () => ({
  cn: (...classes: unknown[]) => classes.filter(Boolean).join(" "),
}));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

afterEach(() => {
  document.body.innerHTML = "";
  vi.resetModules();
  vi.unstubAllGlobals();
});

async function render(node: (mod: typeof import("./index")) => React.ReactNode) {
  const mod = await import("./index");
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  await act(async () => {
    root.render(node(mod));
  });
  return { host, unmount: () => act(() => root.unmount()) };
}

it("falls back to the still CSS sphere, same size, when WebGL is unavailable", async () => {
  // jsdom has no WebGLRenderingContext: the orb never asks for a context.
  vi.stubGlobal("WebGLRenderingContext", undefined);
  const { host, unmount } = await render(({ SuperchatOrb }) => <SuperchatOrb size={96} />);
  const orb = host.querySelector<HTMLElement>("[data-testid=superchat-orb]");
  expect(orb?.dataset.orbFallback).toBe("true");
  expect(orb?.querySelector("canvas")).toBeNull();
  expect(orb?.style.width).toBe("96px");
  expect(orb?.style.height).toBe("96px");
  expect(orb?.getAttribute("aria-hidden")).toBe("true");
  await unmount();
});

it("falls back when a WebGL context cannot be created", async () => {
  vi.stubGlobal("WebGLRenderingContext", function WebGLRenderingContext() {});
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
  const { host, unmount } = await render(({ SuperchatOrb }) => <SuperchatOrb size={34} />);
  expect(host.querySelector<HTMLElement>("[data-testid=superchat-orb]")?.dataset.orbFallback).toBe(
    "true",
  );
  await unmount();
});

it("follows Superchat's live presence unless given its own state", async () => {
  vi.stubGlobal("WebGLRenderingContext", undefined);
  const { host, unmount } = await render(({ SuperchatOrb, SuperchatPresenceProvider }) => (
    <SuperchatPresenceProvider state="working">
      <SuperchatOrb size={22} />
      <SuperchatOrb size={22} state="idle" />
    </SuperchatPresenceProvider>
  ));
  const states = [...host.querySelectorAll<HTMLElement>("[data-testid=superchat-orb]")].map(
    (orb) => orb.dataset.orbState,
  );
  expect(states).toEqual(["working", "idle"]);
  await unmount();
});

it("rests when no presence is provided", async () => {
  vi.stubGlobal("WebGLRenderingContext", undefined);
  const { host, unmount } = await render(({ SuperchatOrb }) => <SuperchatOrb />);
  expect(host.querySelector<HTMLElement>("[data-testid=superchat-orb]")?.dataset.orbState).toBe(
    "idle",
  );
  await unmount();
});

it("hands back no renderer when the shared WebGL context cannot start", async () => {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
  const { registerOrb, resetOrbRendererForTests } = await import("./renderer");
  resetOrbRendererForTests();
  const canvas = document.createElement("canvas");
  expect(registerOrb(canvas, 34, "idle", () => undefined)).toBeNull();
});
