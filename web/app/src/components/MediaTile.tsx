import { cn } from "@superchat/ui-web";
import { FileText, Image as ImageIcon, LayoutTemplate, Presentation } from "lucide-react";
import type { ReactNode, Ref } from "react";
import type { ArtifactKind } from "../lib/artifact-kind";

/** The brand tile's glyph per kind (white on a blue tile). */
const KIND_GLYPH: Record<ArtifactKind, typeof FileText> = {
  page: LayoutTemplate,
  document: FileText,
  deck: Presentation,
  image: ImageIcon,
  file: FileText,
};

/**
 * A result in the Conversation as a media tile (the old design notes "Results"): a dark rounded
 * card with a deep blue glow, the brand line and a big title on the left, art on the right, and
 * a white "Open" pill with a meta line at the bottom. `art` is the real preview when Superchat has
 * one (framed like a window rising from the bottom edge); otherwise `MediaArt` draws one for
 * the kind. Extra actions (download, a menu) sit at the end of the bottom row.
 */
export function MediaTile({
  kind,
  brand,
  title,
  art,
  meta,
  openLabel,
  onOpen,
  openAriaLabel,
  openRef,
  actions,
  badge,
}: {
  /** The Open pill, so a dialog can hand focus back to it. */
  openRef?: Ref<HTMLButtonElement>;
  kind: ArtifactKind;
  brand: string;
  title: string;
  art: ReactNode;
  meta: ReactNode;
  openLabel: string;
  onOpen: () => void;
  openAriaLabel?: string;
  actions?: ReactNode;
  /** A small chip beside the brand (a version). */
  badge?: ReactNode;
}) {
  const Glyph = KIND_GLYPH[kind];
  return (
    <div
      data-testid="media-tile"
      className="superchat-media relative isolate flex min-h-[250px] w-[620px] max-w-full flex-col justify-between overflow-hidden rounded-[28px] px-[22px] pt-[22px] pb-[18px] text-white"
    >
      <div className="relative z-10 min-w-0">
        <span className="flex items-center gap-1.5 text-[15px] font-semibold tracking-[-0.2px] opacity-95">
          <span
            aria-hidden="true"
            data-tone="blue"
            className="superchat-tile size-[22px] rounded-[28%] [&>svg]:size-[13px]"
          >
            <Glyph strokeWidth={2.4} />
          </span>
          {brand}
          {badge}
        </span>
        <h3
          className="mt-1.5 line-clamp-3 max-w-[55%] text-[26px] leading-[1.12] font-semibold tracking-[0.2px] text-balance break-words max-sm:max-w-full max-sm:text-[22px]"
          dir="auto"
        >
          {title}
        </h3>
      </div>
      <div
        aria-hidden="true"
        className="pointer-events-none absolute end-[26px] bottom-0 h-[78%] w-[40%] max-sm:opacity-40"
      >
        {art}
      </div>
      {/* The art owns the tile's right 40%: keep the row (Open, meta, actions) in the left column
          so nothing sits on the preview. On a phone the art is a faint backdrop, so it spans. */}
      <div className="relative z-10 mt-6 flex min-w-0 max-w-[calc(60%-34px)] items-center gap-3 max-sm:max-w-none">
        <button
          ref={openRef}
          type="button"
          onClick={onOpen}
          aria-label={openAriaLabel}
          className="h-10 shrink-0 rounded-full bg-white px-5 text-[15px] font-medium tracking-[-0.24px] text-black transition-[filter] hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
        >
          {openLabel}
        </button>
        <span className="min-w-0 truncate text-[15px] tracking-[-0.24px] text-white/90">
          {meta}
        </span>
        {actions ? (
          <span className="ms-auto flex shrink-0 items-center gap-1.5">{actions}</span>
        ) : null}
      </div>
    </div>
  );
}

/** A small round action on the media tile (Download, More). */
export const MEDIA_ACTION =
  "grid size-9 place-items-center rounded-full bg-white/12 text-white transition-colors hover:bg-white/20 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring [&_svg]:size-4";

/** A real preview, framed like a window rising from the tile's bottom edge. */
export function MediaFrame({ children }: { children: ReactNode }) {
  return (
    <div className="absolute inset-x-0 bottom-0 h-[86%] overflow-hidden rounded-t-[14px] bg-white shadow-[0_0_40px_rgb(0_0_0/0.5)] ring-1 ring-white/10">
      {children}
    </div>
  );
}

/** Generated art for a kind without a preview: bars for a report, stacked slides for a deck,
 * a browser window for a page, a landscape for an image, a sheet stack for any other file. */
export function MediaArt({ kind }: { kind: ArtifactKind }) {
  if (kind === "deck") {
    return (
      <div className="absolute inset-0">
        <span className="superchat-media-bar absolute end-0 bottom-[34%] h-[46%] w-[82%] rounded-[12px]" />
        <span className="superchat-media-bar absolute end-[8%] bottom-[20%] h-[46%] w-[82%] rounded-[12px]" />
        <span className="superchat-media-key absolute end-[16%] bottom-[6%] h-[46%] w-[82%] rounded-[12px]" />
      </div>
    );
  }
  if (kind === "page") {
    return (
      <div className="superchat-media-bar absolute inset-x-0 bottom-0 h-[86%] overflow-hidden rounded-t-[14px]">
        <div className="flex gap-1.5 p-3">
          <span className="size-2 rounded-full bg-white/25" />
          <span className="size-2 rounded-full bg-white/25" />
          <span className="size-2 rounded-full bg-white/25" />
        </div>
        <span className="superchat-media-key mx-3 block h-[34%] rounded-[8px]" />
        <span className="mx-3 mt-3 block h-2 w-[70%] rounded-full bg-white/15" />
        <span className="mx-3 mt-2 block h-2 w-[50%] rounded-full bg-white/15" />
      </div>
    );
  }
  if (kind === "image") {
    return (
      <div className="superchat-media-bar absolute inset-x-0 bottom-0 h-[80%] overflow-hidden rounded-t-[14px]">
        <span className="absolute end-[22%] top-[18%] size-8 rounded-full bg-white/30" />
        <span className="superchat-media-key absolute -start-[10%] -bottom-[30%] h-[80%] w-[80%] rotate-45 rounded-[12px]" />
        <span className="absolute -end-[20%] -bottom-[40%] h-[80%] w-[80%] rotate-45 rounded-[12px] bg-white/15" />
      </div>
    );
  }
  if (kind === "file") {
    return (
      <div className="absolute inset-0">
        <span className="superchat-media-bar absolute end-[18%] bottom-0 h-[72%] w-[56%] -rotate-6 rounded-t-[12px]" />
        <span className="superchat-media-key absolute end-[6%] bottom-0 h-[80%] w-[56%] rounded-t-[12px]" />
      </div>
    );
  }
  // A report: a bar chart with one bright key bar.
  const bars: { height: string; key?: boolean }[] = [
    { height: "44%" },
    { height: "60%" },
    { height: "92%", key: true },
    { height: "56%" },
  ];
  return (
    <div className="absolute inset-0 flex items-end justify-end gap-3">
      {bars.map((bar, index) => (
        <span
          key={index}
          className={cn(
            "w-[34px] rounded-t-[10px]",
            bar.key ? "superchat-media-key" : "superchat-media-bar",
          )}
          style={{ height: bar.height }}
        />
      ))}
    </div>
  );
}
