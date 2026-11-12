import { createContext, type ReactNode, useContext, useSyncExternalStore } from "react";

/** Where Superchat's one orb lives right now (the old design notes "Orb"). */
export type OrbHome = "hero" | "sidebar" | "toolbar";

/**
 * The orb has one home at a time: the big centered orb on the start page; otherwise the
 * sidebar header while the sidebar shows (expanded on desktop, or the drawer open); otherwise
 * the toolbar title.
 */
export function orbPlacement({
  startPage,
  sidebarVisible,
}: {
  startPage: boolean;
  sidebarVisible: boolean;
}): OrbHome {
  if (startPage) return "hero";
  return sidebarVisible ? "sidebar" : "toolbar";
}

const OrbHomeContext = createContext<OrbHome | null>(null);

export function OrbHomeProvider({ home, children }: { home: OrbHome; children: ReactNode }) {
  return <OrbHomeContext.Provider value={home}>{children}</OrbHomeContext.Provider>;
}

/**
 * Whether the orb lives in `slot`. Outside the shell (dev previews, tests) there is no
 * provider: the sidebar and the hero keep their orb and the toolbar shows none.
 */
export function useOrbHome(slot: OrbHome): boolean {
  const home = useContext(OrbHomeContext);
  return home === null ? slot !== "toolbar" : home === slot;
}

const DESKTOP_QUERY = "(min-width: 768px)";

function subscribeDesktop(onChange: () => void): () => void {
  if (typeof window.matchMedia !== "function") return () => undefined;
  const media = window.matchMedia(DESKTOP_QUERY);
  media.addEventListener("change", onChange);
  return () => media.removeEventListener("change", onChange);
}

function desktopSnapshot(): boolean {
  return typeof window.matchMedia === "function" ? window.matchMedia(DESKTOP_QUERY).matches : true;
}

/** True at `md` and up, where the sidebar is a panel rather than a drawer. */
export function useIsDesktop(): boolean {
  return useSyncExternalStore(subscribeDesktop, desktopSnapshot, () => true);
}
