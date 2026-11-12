import { useLingui } from "@lingui/react/macro";
import { Button, cn } from "@superchat/ui-web";
import type { ReactNode } from "react";
import { useFirstRun } from "./useFirstRun";

/**
 * A one-time contextual hint (the old design notes-style: calm, one line, no slideshow):
 * wraps the real element it explains and, the first time that element is seen, shows a
 * small callout under (or over) it with the explanation and a "Got it". Once dismissed —
 * by "Got it" or, for the person who never notices it, simply by moving on — it never
 * shows again for that `hintKey` (`useFirstRun`, per-person).
 *
 * Deliberately not a floating popover library: the things this wraps range from a whole
 * `AskCard` to a bare `ScreenHeader`, and turning arbitrary content into a popover trigger
 * (a `<button>`) would break it. A plain `position: relative` wrapper keeps every wrapped
 * surface exactly as interactive as it already was.
 */
export function FirstRunHint({
  hintKey,
  text,
  variant = "popover",
  side = "bottom",
  align = "start",
  /** False suppresses the hint even if unseen — e.g. a section with no content yet. It is
   * not marked seen, so it can still show once there is something to explain. */
  active = true,
  className,
  children,
}: {
  hintKey: string;
  text: ReactNode;
  /** "inline" sits in the page flow as a quiet caption (for hints that must not cover the
   * thing they describe, like a live preview); "popover" floats over its neighbors. */
  variant?: "popover" | "inline";
  side?: "top" | "bottom";
  align?: "start" | "end";
  active?: boolean;
  className?: string;
  children?: ReactNode;
}) {
  const { t } = useLingui();
  const { seen, markSeen } = useFirstRun(hintKey);
  const show = active && !seen;

  const gotIt = (
    <Button type="button" variant="outline" size="xs" onClick={markSeen} className="shrink-0">
      {t`Got it`}
    </Button>
  );

  if (variant === "inline") {
    return (
      <>
        {children}
        {show ? (
          <div
            role="status"
            data-testid={`first-run-hint-${hintKey}`}
            className={cn(
              "flex items-center justify-between gap-3 rounded-xl bg-muted/60 py-1.5 ps-3 pe-1.5 text-start",
              className,
            )}
          >
            <p className="min-w-0 text-[13px] leading-[1.4] text-muted-foreground">{text}</p>
            {gotIt}
          </div>
        ) : null}
      </>
    );
  }

  return (
    <div className={cn("relative", className)}>
      {children}
      {show ? (
        <div
          role="status"
          data-testid={`first-run-hint-${hintKey}`}
          className={cn(
            "absolute z-40 w-[248px] animate-in fade-in slide-in-from-top-1 rounded-2xl border border-border bg-card p-3 text-start shadow-float motion-reduce:animate-none",
            side === "bottom" ? "top-full mt-2" : "bottom-full mb-2",
            align === "start" ? "start-0" : "end-0",
          )}
        >
          <p className="text-[13px] leading-[1.45] text-foreground">{text}</p>
          <div className="mt-2">{gotIt}</div>
        </div>
      ) : null}
    </div>
  );
}
