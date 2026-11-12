import { Trans } from "@lingui/react/macro";
import { ChevronRight } from "lucide-react";
import { type ReactNode, useState } from "react";

const STORAGE_KEY = "superchat.memory.closedSections";

function readClosed(): string[] {
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "[]");
    return Array.isArray(parsed) ? parsed.filter((id): id is string => typeof id === "string") : [];
  } catch {
    return [];
  }
}

function writeClosed(ids: string[]) {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(ids));
  } catch {
    // Storage can be blocked; the section just won't remember its state.
  }
}

/**
 * One collapsible Memory section: a small uppercase label with its count ("ABOUT YOU · 3")
 * over its rows. Open state is remembered per section id in localStorage.
 */
export function MemorySection({
  id,
  title,
  count,
  forceOpen = false,
  editing = false,
  onEdit,
  children,
}: {
  /** The section's Edit is on. */
  editing?: boolean;
  /** Shows "Edit" / "Done" on the header's right. */
  onEdit?: () => void;
  id: string;
  title: string;
  count?: number;
  /** Stays open while a search is filtering the rows. */
  forceOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(() => !readClosed().includes(id));
  const shown = open || forceOpen;
  const toggle = () => {
    const next = !open;
    setOpen(next);
    const closed = readClosed().filter((closedId) => closedId !== id);
    writeClosed(next ? closed : [...closed, id]);
  };
  return (
    <section className="flex flex-col gap-1.5" data-testid={`memory-section-${id}`}>
      <div className="flex items-center justify-between gap-2 px-1.5">
        <button
          type="button"
          aria-expanded={shown}
          onClick={toggle}
          className="group/header flex min-w-0 items-center gap-1 rounded-md text-left text-[13px] font-semibold tracking-[-0.1px] text-foreground outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
        >
          <span className="truncate">{title}</span>
          {count === undefined ? null : (
            <span className="font-normal text-ink-3 tabular-nums">{count}</span>
          )}
          <ChevronRight
            className={`size-3 shrink-0 text-ink-3 opacity-0 transition-[transform,opacity] group-hover/header:opacity-100 ${shown ? "rotate-90" : ""}`}
            aria-hidden
          />
        </button>
        {onEdit && shown ? (
          <button
            type="button"
            aria-pressed={editing}
            onClick={onEdit}
            className="shrink-0 rounded-md text-[13px] text-link outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50"
          >
            {editing ? <Trans>Done</Trans> : <Trans>Edit</Trans>}
          </button>
        ) : null}
      </div>
      {shown ? <div className="flex flex-col">{children}</div> : null}
    </section>
  );
}

/** The one muted line an empty section shows. */
export function MemoryEmptyLine({ children }: { children: ReactNode }) {
  return <p className="px-1.5 py-1 text-[12px] leading-4 text-ink-3">{children}</p>;
}
