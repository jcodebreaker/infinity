import type { ReactNode } from "react";

/** Hover-capable pointers only: touch keeps the rail beside the bubble. The `after:` strip is
 * an invisible bridge over the gap under the toolbar, so moving up from the bubble keeps it. */
const ABOVE =
  "[@media(hover:hover)_and_(pointer:fine)]:top-auto [@media(hover:hover)_and_(pointer:fine)]:bottom-full [@media(hover:hover)_and_(pointer:fine)]:mb-1 [@media(hover:hover)_and_(pointer:fine)]:translate-y-0 [@media(hover:hover)_and_(pointer:fine)]:rounded-full [@media(hover:hover)_and_(pointer:fine)]:border [@media(hover:hover)_and_(pointer:fine)]:border-line [@media(hover:hover)_and_(pointer:fine)]:bg-card [@media(hover:hover)_and_(pointer:fine)]:px-1 [@media(hover:hover)_and_(pointer:fine)]:shadow-float [@media(hover:hover)_and_(pointer:fine)]:after:absolute [@media(hover:hover)_and_(pointer:fine)]:after:inset-x-0 [@media(hover:hover)_and_(pointer:fine)]:after:top-full [@media(hover:hover)_and_(pointer:fine)]:after:h-2 [@media(hover:hover)_and_(pointer:fine)]:after:content-['']";
const ABOVE_END =
  "[@media(hover:hover)_and_(pointer:fine)]:start-0 [@media(hover:hover)_and_(pointer:fine)]:ms-0";
const ABOVE_START =
  "[@media(hover:hover)_and_(pointer:fine)]:end-0 [@media(hover:hover)_and_(pointer:fine)]:me-0";

export function MessageHoverMetadata({
  side,
  pinned = false,
  above = false,
  children,
}: {
  side: "start" | "end";
  pinned?: boolean;
  /** A small floating toolbar above the bubble instead of the rail beside it. */
  above?: boolean;
  children: ReactNode;
}) {
  // Touch exposes More; hover-capable pointers reveal the full rail on demand.
  const reveal = pinned
    ? "pointer-events-auto opacity-100"
    : "pointer-events-auto opacity-100 [@media(hover:hover)_and_(pointer:fine)]:pointer-events-none [@media(hover:hover)_and_(pointer:fine)]:opacity-0 [@media(hover:hover)_and_(pointer:fine)]:group-hover/message:pointer-events-auto [@media(hover:hover)_and_(pointer:fine)]:group-hover/message:opacity-100 [@media(hover:hover)_and_(pointer:fine)]:focus-within:pointer-events-auto [@media(hover:hover)_and_(pointer:fine)]:focus-within:opacity-100";

  return (
    <div
      data-testid="message-hover-rail"
      className={`absolute top-1/2 z-10 flex -translate-y-1/2 items-center transition-opacity ${reveal} ${
        side === "end" ? "start-full ms-1" : "end-full me-1"
      }${above ? ` ${ABOVE} ${side === "end" ? ABOVE_END : ABOVE_START}` : ""}`}
    >
      {children}
    </div>
  );
}
