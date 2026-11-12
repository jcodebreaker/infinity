import { cn } from "@superchat/ui-web";
import type { LucideIcon } from "lucide-react";

/** The rounded muted tile an Activity's source icon sits in. A live run's icon breathes
 * (`motion-safe`, so reduced motion shows it still); a failed one takes the destructive
 * token. */
export function ActivityIconTile({
  Icon,
  live,
  failed,
  size = "md",
}: {
  Icon: LucideIcon;
  live: boolean;
  failed: boolean;
  size?: "sm" | "md" | "lg";
}) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "flex shrink-0 items-center justify-center rounded-lg",
        size === "lg" ? "size-10" : size === "sm" ? "size-7" : "size-9",
        failed ? "bg-destructive/10 text-destructive" : "bg-muted text-muted-foreground",
      )}
    >
      <Icon
        size={size === "lg" ? 19 : size === "sm" ? 14 : 17}
        strokeWidth={1.75}
        className={cn(live && "motion-safe:animate-[rkPulse_2.4s_ease-in-out_infinite]")}
      />
    </span>
  );
}
