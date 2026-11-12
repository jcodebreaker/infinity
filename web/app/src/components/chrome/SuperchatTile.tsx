import { cn } from "@superchat/ui-web";
import type { ReactNode } from "react";

export type TileTone =
  | "blue"
  | "green"
  | "yellow"
  | "orange"
  | "purple"
  | "red"
  | "indigo"
  | "teal"
  | "gray";

/** A colored app tile (the old design notes "Tiles"): a rounded square with a soft gradient
 * (`tile-*` tokens, `.superchat-tile` in styles.css) and a white glyph. */
export function SuperchatTile({
  tone,
  size = 28,
  className,
  children,
}: {
  tone: TileTone;
  size?: number;
  className?: string;
  children: ReactNode;
}) {
  return (
    <span
      aria-hidden="true"
      data-tone={tone}
      className={cn("superchat-tile", className)}
      style={{ width: size, height: size }}
    >
      {children}
    </span>
  );
}
