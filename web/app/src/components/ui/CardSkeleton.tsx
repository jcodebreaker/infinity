import { t } from "@lingui/core/macro";

/**
 * Quiet placeholder blocks for a loading list of cards — no "Loading…" text
 * (the old design notes, "Motion" and the redesign brief's loading-state rule).
 */
export function CardSkeletonList({ count = 3 }: { count?: number }) {
  return (
    <div className="flex flex-col gap-3">
      <span className="sr-only" role="status">
        {t`Loading…`}
      </span>
      {Array.from({ length: count }, (_, index) => (
        <div key={index} aria-hidden="true" className="h-24 animate-pulse rounded-2xl bg-muted" />
      ))}
    </div>
  );
}
