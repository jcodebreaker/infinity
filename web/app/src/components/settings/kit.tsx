import { cn } from "@superchat/ui-web";
import type { ReactNode } from "react";
import { ChevronGlyph } from "../chrome/SuperchatGlyphs";
import { SuperchatTile, type TileTone } from "../chrome/SuperchatTile";
import { MUSE_INSET_GROUP } from "../ui";

/**
 * iOS-style settings pieces: an inset group of rows with hairline dividers, a quiet caption
 * above and an explanation below. Every Settings section is built from these, so they all
 * read like the Superchat section.
 */
export function SettingsGroup({
  title,
  footer,
  children,
}: {
  title?: ReactNode;
  footer?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section>
      {title ? (
        <h3 className="px-1.5 pb-1.5 text-[13px] font-semibold tracking-[-0.1px] text-foreground">
          {title}
        </h3>
      ) : null}
      <div className={MUSE_INSET_GROUP}>{children}</div>
      {footer ? <p className="px-1.5 pt-1.5 text-[12px] text-ink-3">{footer}</p> : null}
    </section>
  );
}

export const SETTINGS_ROW =
  "superchat-row flex min-h-[52px] w-full items-center justify-between gap-3 px-3 py-2 text-[14px] tracking-[-0.15px]";

/** A row's colored tile (the old design notes "Settings"). */
export type SettingsIcon = { tone: TileTone; glyph: ReactNode };

/** The row's lead: its tile, then the label with an optional quiet line under it. */
function RowLead({ icon, label, sub }: { icon?: SettingsIcon; label: ReactNode; sub?: ReactNode }) {
  return (
    <span className="flex min-w-0 items-center gap-3">
      {icon ? (
        <SuperchatTile tone={icon.tone} size={28}>
          {icon.glyph}
        </SuperchatTile>
      ) : null}
      <span className="min-w-0">
        <span className="block truncate text-foreground">{label}</span>
        {sub ? <span className="block truncate text-[12px] text-ink-3">{sub}</span> : null}
      </span>
    </span>
  );
}

/** A label on the left and a value (or control) on the right. */
export function SettingsRow({
  label,
  sub,
  icon,
  value,
  children,
}: {
  label: ReactNode;
  sub?: ReactNode;
  icon?: SettingsIcon;
  value?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className={cn(SETTINGS_ROW, !icon && "superchat-row-plain")}>
      <RowLead icon={icon} label={label} sub={sub} />
      {value !== undefined ? (
        <span className="min-w-0 truncate text-[13.5px] text-ink-3">{value}</span>
      ) : null}
      {children}
    </div>
  );
}

/** A row that opens something: a chevron, the whole row tappable. */
export function SettingsLinkRow({
  label,
  sub,
  icon,
  value,
  onClick,
  expanded,
}: {
  label: ReactNode;
  sub?: ReactNode;
  icon?: SettingsIcon;
  value?: ReactNode;
  onClick: () => void;
  /** For rows that expand in place rather than navigate. */
  expanded?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-expanded={expanded}
      className={cn(
        SETTINGS_ROW,
        !icon && "superchat-row-plain",
        "group text-start transition-colors hover:bg-selection focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring",
      )}
    >
      <RowLead icon={icon} label={label} sub={sub} />
      <span className="flex min-w-0 items-center gap-2 text-[13.5px] text-ink-3">
        {value ? <span className="truncate">{value}</span> : null}
        <ChevronGlyph
          className={cn(
            "h-[13px] w-2 shrink-0 opacity-60 transition-transform",
            expanded ? "rotate-90" : "group-hover:translate-x-0.5",
          )}
        />
      </span>
    </button>
  );
}

/** A pill segmented control, same as the Superchat section's "Working on its own". */
export function SettingsSegmented<T extends string>({
  label,
  options,
  value,
  onChange,
  testId,
  disabled,
}: {
  label: string;
  options: readonly { value: T; label: string }[];
  value: T;
  onChange: (next: T) => void;
  testId?: string;
  disabled?: boolean;
}) {
  return (
    <div className="p-2">
      <fieldset
        aria-label={label}
        data-testid={testId}
        className="m-0 grid rounded-lg border-0 bg-selection p-0.5"
        style={{ gridTemplateColumns: `repeat(${options.length}, minmax(0, 1fr))` }}
      >
        {options.map((option) => {
          const selected = option.value === value;
          return (
            <button
              key={option.value}
              type="button"
              aria-pressed={selected}
              disabled={disabled}
              data-testid={testId ? `${testId}-${option.value}` : undefined}
              onClick={() => {
                if (!selected) onChange(option.value);
              }}
              className={cn(
                "h-7 rounded-md text-[13px] transition-[background-color,box-shadow,color] duration-200 focus-visible:outline-2 focus-visible:outline-ring disabled:cursor-default disabled:opacity-60",
                selected
                  ? "bg-group text-foreground shadow-[0_1px_2px_rgb(0_0_0/0.14)]"
                  : "text-ink-2 hover:text-foreground",
              )}
            >
              {option.label}
            </button>
          );
        })}
      </fieldset>
    </div>
  );
}
