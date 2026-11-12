import { cn } from "@superchat/ui-web";

/** The superchat mark: two overlapping circle outlines and a dot. */
export function SuperchatMark({ size = 34, className }: { size?: number; className?: string }) {
  return (
    <svg
      aria-hidden="true"
      width={size}
      height={size}
      viewBox="0 0 34 34"
      className={cn("shrink-0", className)}
    >
      <circle cx="13" cy="17" r="10" fill="none" strokeWidth="2.5" className="stroke-tint-ink" />
      <circle
        cx="21"
        cy="17"
        r="10"
        fill="none"
        strokeWidth="2.5"
        strokeOpacity="0.7"
        className="stroke-foreground"
      />
      <circle cx="17" cy="17" r="2.6" className="fill-tint-ink" />
    </svg>
  );
}

/** The mark with the lowercase wordmark beside it. */
export function SuperchatLogo({ className }: { className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-2.5", className)}>
      <SuperchatMark />
      <span className="text-[26px] leading-none font-[650] tracking-[-0.03em] text-foreground">
        superchat
      </span>
    </span>
  );
}
