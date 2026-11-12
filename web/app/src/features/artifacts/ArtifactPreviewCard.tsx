import { cn } from "@superchat/ui-web";
import type { MouseEvent, ReactNode, RefObject } from "react";
import { useCallback, useState } from "react";
import { ArtifactPreviewThumbnail, type PreviewableArtifact } from "./ArtifactPreviewThumbnail";

export type ArtifactPreviewCardSize = "compact" | "grid";

/**
 * The one artifact preview card — a finished-feeling piece of work being handed to you,
 * not a file-manager row. Shared by the Library grid (`size="grid"`, fills its cell) and
 * the chat file card (`size="compact"`, a fixed but responsive width): same radius,
 * hairline border, edge-to-edge 16:10 preview with the title and meta under it, and a small
 * glass cluster of actions top-right that appears on hover/focus (always visible on touch).
 */
export function ArtifactPreviewCard({
  artifact,
  title,
  meta,
  size,
  buttonLabel,
  buttonRef,
  onOpen,
  actions,
  badge,
  fallback,
  testId,
}: {
  /** Art for a kind without a real preview (LibraryArt). */
  fallback?: ReactNode;
  artifact: PreviewableArtifact;
  title: string;
  meta: ReactNode;
  size: ArtifactPreviewCardSize;
  buttonLabel: string;
  buttonRef?: RefObject<HTMLButtonElement | null>;
  onOpen: () => void;
  /** Rendered top-right, over the preview; each action should be a small round glass button. */
  actions?: ReactNode;
  testId?: string;
  /** A small state marker pinned top-left over the preview (e.g. "Published"). */
  badge?: ReactNode;
}) {
  const [loaded, setLoaded] = useState(false);
  const [detail, setDetail] = useState<string | null>(null);
  const markLoaded = useCallback(() => setLoaded(true), []);

  return (
    <div
      className={cn(
        "group relative flex min-w-0 flex-col",
        size === "compact" && "w-[460px] max-w-full shrink-0",
      )}
    >
      <button
        ref={buttonRef}
        type="button"
        aria-label={buttonLabel}
        onClick={onOpen}
        data-testid={testId}
        className="flex min-w-0 flex-col gap-2.5 rounded-xl text-start outline-none focus-visible:outline-2 focus-visible:outline-ring focus-visible:outline-offset-2"
      >
        <div className="relative aspect-[16/10] w-full overflow-hidden rounded-xl bg-window shadow-none transition-[translate,box-shadow] duration-200 group-hover:-translate-y-0.5 group-hover:shadow-float motion-reduce:transition-none">
          <div
            aria-hidden="true"
            className={cn(
              "absolute inset-0 bg-window transition-opacity duration-300 motion-safe:animate-pulse",
              loaded ? "opacity-0" : "opacity-100",
            )}
          />
          <div
            className={cn(
              "absolute inset-0 transition-opacity duration-300",
              loaded ? "opacity-100" : "opacity-0",
            )}
          >
            <ArtifactPreviewThumbnail
              artifact={artifact}
              fallback={fallback}
              onReady={markLoaded}
              onMeta={setDetail}
            />
          </div>
          {/* The hairline sits above the preview, so a white slide still has an edge. */}
          <div
            aria-hidden="true"
            className="pointer-events-none absolute inset-0 rounded-xl ring-[0.5px] ring-separator ring-inset"
          />
        </div>
        <div className="min-w-0 px-0.5">
          <p
            className="line-clamp-2 text-[13px] leading-[1.3] font-medium text-foreground"
            dir="auto"
          >
            {title}
          </p>
          <p className="mt-0.5 truncate text-[12px] text-ink-3">
            {detail ? (
              <>
                {meta} · {detail}
              </>
            ) : (
              meta
            )}
          </p>
        </div>
      </button>
      {badge ? (
        <div className="pointer-events-none absolute start-2 top-2 z-10">{badge}</div>
      ) : null}
      {actions ? (
        <div className="superchat-glass-pill absolute end-2 top-2 z-10 flex items-center gap-px rounded-full p-0.5 opacity-0 transition-opacity duration-150 group-hover:opacity-100 group-focus-within:opacity-100 [@media(hover:none)]:opacity-100">
          {actions}
        </div>
      ) : null}
    </div>
  );
}

/** Shared look for a small round action button inside the card's glass cluster; export it for
 *  triggers (e.g. a dropdown menu trigger) that need the same chrome but not `GlassAction`'s onClick. */
export const glassActionClassName =
  "grid size-7 shrink-0 place-items-center rounded-full text-ink-2 transition-colors hover:bg-selection hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring focus-visible:-outline-offset-2";

/** A small round action button, meant to sit in `ArtifactPreviewCard`'s glass actions cluster. */
export function GlassAction({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: (event: MouseEvent) => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={(event) => {
        event.stopPropagation();
        onClick(event);
      }}
      className={glassActionClassName}
    >
      {children}
    </button>
  );
}
