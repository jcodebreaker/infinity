import { t } from "@lingui/core/macro";

/**
 * Loading state for a context-panel tab (Activity, Memory): quiet skeleton lines shaped
 * like the rows they stand in for — a mark, a title bar, a subtitle bar, a time bar —
 * never a lone spinner.
 */
export function PanelRowSkeletonList({ count = 4 }: { count?: number }) {
  return (
    <div className="flex flex-col gap-1" data-testid="panel-skeleton">
      <span className="sr-only" role="status">
        {t`Loading…`}
      </span>
      {Array.from({ length: count }, (_, index) => (
        <div key={index} aria-hidden="true" className="flex items-start gap-3 px-2.5 py-2">
          <div className="mt-[3px] size-[15px] shrink-0 animate-pulse rounded-full bg-muted" />
          <div className="min-w-0 flex-1 space-y-1.5">
            <div className="h-3 w-[70%] animate-pulse rounded bg-muted" />
            <div className="h-2.5 w-[45%] animate-pulse rounded bg-muted" />
          </div>
          <div className="h-2.5 w-8 shrink-0 animate-pulse rounded bg-muted" />
        </div>
      ))}
    </div>
  );
}
