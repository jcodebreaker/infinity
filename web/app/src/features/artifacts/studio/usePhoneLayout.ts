import { useSyncExternalStore } from "react";

/** Under 640px the studio stacks (ADR 0024 "Web"): canvas, slide strip, chat as a sheet. */
const PHONE_QUERY = "(max-width: 639px)";

function subscribe(onChange: () => void): () => void {
  if (typeof window.matchMedia !== "function") return () => undefined;
  const media = window.matchMedia(PHONE_QUERY);
  media.addEventListener("change", onChange);
  return () => media.removeEventListener("change", onChange);
}

const snapshot = (): boolean =>
  typeof window.matchMedia === "function" && window.matchMedia(PHONE_QUERY).matches;

/** True while the window is phone width. */
export function usePhoneLayout(): boolean {
  return useSyncExternalStore(subscribe, snapshot, () => false);
}
