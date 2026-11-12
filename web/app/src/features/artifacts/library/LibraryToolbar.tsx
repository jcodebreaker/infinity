import { useLingui } from "@lingui/react/macro";
import { cn } from "@superchat/ui-web";
import { Search } from "lucide-react";
import type { ReactNode } from "react";

/** A quiet, compact Mac-style search field: a soft recessed fill, no border, capped width. */
export function LibrarySearchField({
  value,
  onChange,
  className,
}: {
  value: string;
  onChange: (value: string) => void;
  className?: string;
}) {
  const { t } = useLingui();
  return (
    <label className={cn("relative block w-full max-w-[360px]", className)}>
      <Search
        size={14}
        strokeWidth={1.9}
        aria-hidden="true"
        className="pointer-events-none absolute start-3 top-1/2 -translate-y-1/2 text-ink-3"
      />
      <input
        type="search"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={t`Search your Library…`}
        aria-label={t`Search your Library`}
        className="h-9 w-full rounded-[10px] bg-selection ps-8.5 pe-3 text-[13px] text-foreground outline-none placeholder:text-ink-3 focus-visible:ring-1 focus-visible:ring-ring focus-visible:ring-inset"
      />
    </label>
  );
}

export type FilterChip<V extends string> = { value: V; label: ReactNode; count: number };

/**
 * The Library's filter as small pills sized to their labels (Photos/Finder style), counts in a
 * muted tone. A row of toggle buttons with one pressed, so a screen reader hears the choice.
 */
export function FilterChips<V extends string>({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: V;
  onChange: (value: V) => void;
  options: FilterChip<V>[];
}) {
  return (
    <fieldset
      aria-label={label}
      className="m-0 flex min-w-0 flex-wrap items-center gap-1 border-0 p-0"
    >
      {options.map((option) => {
        const selected = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={selected}
            onClick={() => onChange(option.value)}
            className={cn(
              "inline-flex h-7 items-center gap-1.5 rounded-full px-3 text-[12.5px] transition-colors focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring",
              selected
                ? "bg-selection font-medium text-foreground ring-[0.5px] ring-separator ring-inset"
                : "text-ink-2 hover:bg-selection hover:text-foreground",
            )}
          >
            {option.label} <span className="text-ink-3 tabular-nums">{option.count}</span>
          </button>
        );
      })}
    </fieldset>
  );
}
