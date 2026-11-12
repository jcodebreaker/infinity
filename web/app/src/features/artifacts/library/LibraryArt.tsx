import type { ArtifactKind } from "../../../lib/artifact-kind";

/**
 * A Library preview drawn for a kind without a real thumbnail (the old design notes
 * "Library"): a report as a small dark result tile with its name, a deck as a bright slide, a
 * page or file as a sheet of paper with text lines, an image as a soft landscape.
 */
export function LibraryArt({ kind, name }: { kind: ArtifactKind; name: string }) {
  if (kind === "document") {
    return (
      <span className="superchat-media flex size-full items-start p-3 text-start text-[13px] leading-tight font-semibold text-white">
        <span className="line-clamp-3" dir="auto">
          {name}
        </span>
      </span>
    );
  }
  if (kind === "deck") {
    return (
      <span className="grid size-full place-items-center">
        <span
          data-tone="blue"
          className="superchat-tile flex aspect-[16/10] w-[82%] items-end justify-start rounded-md p-2.5 text-start text-[12px] font-semibold text-white shadow-[0_2px_8px_rgb(0_0_0/0.18)]"
        >
          <span className="line-clamp-2" dir="auto">
            {name}
          </span>
        </span>
      </span>
    );
  }
  if (kind === "image") {
    return (
      <span className="relative block size-full overflow-hidden">
        <span className="absolute end-[22%] top-[18%] size-6 rounded-full bg-sig-ideas/70" />
        <span
          data-tone="green"
          className="superchat-tile absolute -start-[10%] -bottom-[35%] h-[80%] w-[80%] rotate-45 rounded-xl"
        />
        <span
          data-tone="teal"
          className="superchat-tile absolute -end-[20%] -bottom-[45%] h-[80%] w-[80%] rotate-45 rounded-xl"
        />
      </span>
    );
  }
  // A page or any other file: a sheet of paper.
  return (
    <span className="grid size-full place-items-center">
      <span className="flex h-[80%] w-[60%] flex-col gap-[5px] rounded-[4px] bg-group p-2.5 shadow-[0_2px_8px_rgb(0_0_0/0.12)]">
        <i className="h-1.5 w-[60%] rounded-full bg-foreground" />
        <i className="h-1 rounded-full bg-ink-3/40" />
        <i className="h-1 w-[70%] rounded-full bg-ink-3/40" />
        <i className="h-1 rounded-full bg-ink-3/40" />
        <i className="h-1 w-[70%] rounded-full bg-ink-3/40" />
      </span>
    </span>
  );
}
