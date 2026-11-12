import type { Activity } from "@superchat/contracts";
import { cn } from "@superchat/ui-web";

/** The run page's status dot: breathing (in progress), a quiet muted dot (done /
 * cancelled), or — the one allowed color — the destructive token on the dot alone when
 * failed. Nothing else on the run page carries status color (lead review: "remove
 * green/red"; the pill's container and label stay neutral). */
export function RunStatusDot({ status }: { status: Activity["status"] }) {
  if (status === "in_progress") {
    return (
      <span aria-hidden="true" className="relative flex size-2 items-center justify-center">
        <span className="absolute inline-flex size-full animate-ping rounded-full bg-foreground/50 opacity-50 motion-reduce:animate-none" />
        <span className="relative size-1.5 rounded-full bg-foreground motion-safe:animate-[rkPulse_2.4s_ease-in-out_infinite]" />
      </span>
    );
  }
  return (
    <span
      aria-hidden="true"
      className={cn(
        "size-1.5 rounded-full",
        status === "failed" ? "bg-destructive" : "bg-muted-foreground/60",
      )}
    />
  );
}
