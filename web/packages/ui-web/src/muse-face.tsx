import type { MuseState } from "@superchat/contracts";
import { type ComponentType, createContext, type ReactNode, useContext } from "react";

export interface LiveMuseFaceProps {
  color: string;
  size: number;
  state: MuseState;
  /** Open-Ask count; the live face keeps the same badge as the static one. */
  waitingCount: number;
  identity: string;
  className?: string;
}

// A host app (web/app) can supply a richer renderer for the Muse's face, such as the 3D Bloop.
// `BotAvatar face="muse"` uses it when present and draws the static SVG face otherwise, so
// every surface that shows the Muse picks it up without changing its own call.
const LiveMuseFaceContext = createContext<ComponentType<LiveMuseFaceProps> | null>(null);

export function LiveMuseFaceProvider({
  children,
  value,
}: {
  children: ReactNode;
  value: ComponentType<LiveMuseFaceProps>;
}) {
  return <LiveMuseFaceContext value={value}>{children}</LiveMuseFaceContext>;
}

export function useLiveMuseFace(): ComponentType<LiveMuseFaceProps> | null {
  return useContext(LiveMuseFaceContext);
}
