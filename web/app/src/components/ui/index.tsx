import { cn } from "@superchat/ui-web";
import type { LucideIcon } from "lucide-react";
import type { ComponentProps, ReactNode } from "react";
import { type IllustrationKey, illustrationUrl } from "../../lib/illustrations";

// Shared building blocks for the Muse screens (the old design notes). Every Muse screen
// composes these so spacing, type, and surfaces stay identical across Conversation,
// Library, and the Inbox. Colors come only from the semantic
// tokens; type comes only from the scale below.

/**
 * The one Muse type scale. Instrument Sans everywhere (it's the edition's inherited
 * body font — never `font-display`, which is reserved for the signed-out welcome and
 * auth screens). Geist Mono (`label`) is for small tags only, never a whole heading.
 */
/** iOS-style inset grouped list: one rounded card whose rows are split by inset hairlines
 * (`.superchat-group` in styles.css; rows that take the inset separator carry `superchat-row`). */
export const MUSE_INSET_GROUP = "superchat-group";

/** One row of an inset grouped list: 52px, a 28px tile, title over a quiet subtitle. */
export const MUSE_LIST_ROW =
  "superchat-row grid min-h-[52px] w-full grid-cols-[28px_minmax(0,1fr)_auto] items-center gap-x-3 px-3 py-2 text-start text-[14px] tracking-[-0.15px]";

export const MUSE_TYPE = {
  /** The screen's name in the shared top chrome bar (`ScreenHeader`). */
  chromeTitle: "text-[15px] font-semibold tracking-[-0.2px] text-foreground",
  /** The empty Conversation's greeting: a calm start-page headline, balanced over two lines. */
  heroTitle:
    "text-[28px] font-semibold leading-[1.15] tracking-[-0.02em] text-balance text-foreground",
  /** A big in-content heading for a screen that reads like its own page (Ideas). */
  pageTitle: "text-[28px] font-bold leading-[1.1] tracking-[0.2px] text-foreground",
  /** A one-line page subtitle under `pageTitle`, in the Muse's own voice. */
  pageSubtitle: "text-[14px] leading-[1.45] text-ink-2",
  /** A group heading inside a screen ("Productivity", "Paused", "Plan"). */
  sectionTitle: "text-[13px] font-semibold tracking-[-0.1px] text-foreground",
  /** A card's or row's own title (a Goal, a Post, a Library item, an Idea). */
  cardTitle: "text-[16px] font-semibold leading-snug text-foreground",
  /** Regular reading text inside a card or row. */
  body: "text-[15px] leading-[1.55] text-foreground/90",
  /** A quiet fact line — dates, counts, sources. */
  meta: "text-[13px] text-muted-foreground",
  /** A small mono label, sparingly (a card's type tag) — never a whole heading. */
  label: "font-mono text-[11px] font-medium uppercase tracking-[0.08em] text-muted-foreground",
} as const;

/** Centered reading column shared by every Muse screen. */
export function MuseColumn({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn("mx-auto flex w-full max-w-[720px] flex-col px-5 sm:px-8", className)}
      {...props}
    />
  );
}

/** The column the Apple-style screens share: 860px, centered, 28px sides. */
export function MuseWideCenter({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn(
        "mx-auto flex w-full max-w-[860px] flex-col gap-[18px] px-5 pt-1 pb-10 sm:px-7",
        className,
      )}
      {...props}
    />
  );
}

/**
 * A screen's hero (the old design notes "Screens"): the section's colored tile, a 28px bold
 * title over one quiet line, and an optional control on the right.
 */
export function ScreenHero({
  tile,
  title,
  subtitle,
  action,
}: {
  tile: ReactNode;
  title: ReactNode;
  subtitle?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-center gap-3.5 pt-1.5">
      {tile}
      <div className="min-w-0 flex-1">
        <h1 className={MUSE_TYPE.pageTitle}>{title}</h1>
        {subtitle ? <p className={cn("mt-0.5", MUSE_TYPE.pageSubtitle)}>{subtitle}</p> : null}
      </div>
      {action ? <div className="flex shrink-0 items-center gap-2">{action}</div> : null}
    </header>
  );
}

/** A Mac segmented control: one quiet track, the selected segment raised. */
export function SegmentedControl<T extends string>({
  label,
  options,
  value,
  onChange,
  className,
}: {
  label: string;
  options: readonly { value: T; label: ReactNode }[];
  value: T;
  onChange: (next: T) => void;
  className?: string;
}) {
  return (
    <fieldset
      aria-label={label}
      className={cn("m-0 flex min-w-0 rounded-lg border-0 bg-selection p-0.5", className)}
    >
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={option.value === value}
          onClick={() => {
            if (option.value !== value) onChange(option.value);
          }}
          className={cn(
            "h-6 min-w-0 flex-1 truncate rounded-md px-3 text-[12px] whitespace-nowrap transition-colors focus-visible:outline-2 focus-visible:outline-ring",
            option.value === value
              ? "bg-group text-foreground shadow-[0_1px_2px_rgb(0_0_0/0.14)]"
              : "text-ink-2 hover:text-foreground",
          )}
        >
          {option.label}
        </button>
      ))}
    </fieldset>
  );
}

/** A small accent button for a screen's one primary action (the hero's "New goal"). */
export const ACCENT_BUTTON =
  "inline-flex h-8 items-center gap-1.5 rounded-full bg-tint px-3.5 text-[13px] font-medium text-white transition-[filter] hover:brightness-110 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:opacity-60";

/** A quiet, equal-weight secondary button (a decision's "Not now"). */
export const QUIET_BUTTON =
  "inline-flex h-[34px] items-center justify-center rounded-xl bg-selection px-3 text-[14px] font-medium text-foreground transition-[filter] hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:opacity-60";

/** Its accent counterpart (a decision's primary choice). */
export const PRIMARY_BUTTON =
  "inline-flex h-[34px] items-center justify-center rounded-xl bg-tint px-3 text-[14px] font-medium text-white transition-[filter] hover:brightness-110 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:opacity-60";

/**
 * Wide, left-aligned layout for screens that fill the panel instead of reading like a
 * document (Goals, Feed, Library): generous left padding, capped at ~1120px so lines don't
 * run edge-to-edge on a big screen, but never centered — using the space is the point.
 */
export function MuseWideColumn({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn("flex w-full max-w-[1120px] flex-col pl-5 pr-5 sm:pl-12 sm:pr-8", className)}
      {...props}
    />
  );
}

/**
 * Scroll container for a Muse screen: an optional fixed `header` (the shared
 * `ScreenHeader` chrome, outside the scroll like the Conversation's), then a quiet
 * scrolling body with generous bottom room. Content fades under the top edge once
 * scrolled (`data-fade-top` + `data-scrolled`, styles.css) — the same mask the floating
 * Conversation chrome uses over its transcript, so every section shares the effect.
 */
export function MuseScreen({
  header,
  className,
  children,
  onScroll,
  ...props
}: ComponentProps<"div"> & { header?: ReactNode }) {
  return (
    <div className="flex h-full min-w-0 flex-col">
      {header}
      <div
        data-fade-top=""
        onScroll={(event) => {
          event.currentTarget.dataset.scrolled = String(event.currentTarget.scrollTop > 4);
          onScroll?.(event);
        }}
        className={cn("sc-scroll min-w-0 flex-1 overflow-y-auto pb-24", className)}
        {...props}
      >
        {children}
      </div>
    </div>
  );
}

/**
 * The one top chrome bar, shared by every Muse section (the old design notes): same
 * height, padding and hairline border as the Conversation's, a title, optional inline
 * `meta` next to it (a status pill, a count), and a right-side `actions` slot.
 */
export function ScreenHeader({
  title,
  meta,
  actions,
  /** True for a header that also serves as the desktop window's drag region. */
  dragRegion = false,
}: {
  title: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  dragRegion?: boolean;
}) {
  return (
    <header
      className={cn(
        "flex h-14 shrink-0 items-center justify-between gap-3 ps-5 pe-4",
        dragRegion && "app-drag",
      )}
    >
      <div className="flex min-w-0 items-center gap-2.5">
        <span className={cn("truncate", MUSE_TYPE.chromeTitle)}>{title}</span>
        {meta}
      </div>
      {actions ? (
        <div className={cn("flex shrink-0 items-center gap-2", dragRegion && "app-no-drag")}>
          {actions}
        </div>
      ) : null}
    </header>
  );
}

/** Small uppercase label above a group or inside a card. */
export function Eyebrow({ className, ...props }: ComponentProps<"span">) {
  return (
    <span
      className={cn(
        "font-mono text-[11.5px] font-medium uppercase tracking-[0.08em] text-muted-foreground",
        className,
      )}
      {...props}
    />
  );
}

/** Section inside a screen: a plain sentence-case title and its content. */
export function Section({
  title,
  action,
  className,
  children,
}: {
  title?: ReactNode;
  action?: ReactNode;
  className?: string;
  children: ReactNode;
}) {
  return (
    <section className={cn("flex flex-col gap-3", className)}>
      {title || action ? (
        <div className="flex items-center justify-between gap-3">
          {title ? <h2 className={MUSE_TYPE.sectionTitle}>{title}</h2> : <span />}
          {action}
        </div>
      ) : null}
      {children}
    </section>
  );
}

type SurfaceTone = "default" | "attention" | "quiet";

/**
 * The one card surface (`.superchat-card`: 18px, the `group` fill, a half-pixel hairline).
 * `attention` reads the same (what waits on the person carries an orange tile instead);
 * `quiet` sits on the window fill.
 */
export function Surface({
  tone = "default",
  interactive = false,
  className,
  ...props
}: ComponentProps<"div"> & { tone?: SurfaceTone; interactive?: boolean }) {
  return (
    <div
      className={cn(
        tone === "default" && "superchat-card",
        tone === "attention" && "superchat-card",
        tone === "quiet" && "rounded-[18px] bg-window",
        interactive &&
          "cursor-pointer transition-[background-color] duration-150 hover:bg-selection focus-visible:outline-2 focus-visible:outline-ring",
        className,
      )}
      {...props}
    />
  );
}

type PillTone = "neutral" | "live" | "attention" | "done";

/** Rounded status pill with a leading dot. `live` pulses gently. */
export function StatusPill({
  tone = "neutral",
  className,
  onClick,
  label,
  children,
}: {
  tone?: PillTone;
  className?: string;
  /** Makes the pill a button, e.g. to open what's waiting. */
  onClick?: () => void;
  /** Accessible name when the pill is a button. */
  label?: string;
  children: ReactNode;
}) {
  const content = (
    <>
      <span
        aria-hidden="true"
        className="relative flex size-2 shrink-0 items-center justify-center"
      >
        {tone === "attention" || tone === "live" ? (
          <span
            className={cn(
              "absolute inline-flex size-full animate-ping rounded-full opacity-60 motion-reduce:animate-none",
              tone === "attention" ? "bg-warn" : "bg-ok",
            )}
          />
        ) : null}
        <span
          className={cn(
            "relative size-1.5 rounded-full",
            tone === "neutral" && "bg-ink-3",
            tone === "live" && "bg-ok",
            tone === "attention" && "bg-warn",
            tone === "done" && "bg-ok",
          )}
        />
      </span>
      {children}
    </>
  );
  const classes = cn(
    "inline-flex h-[22px] min-w-0 items-center gap-1.5 rounded-full px-2.5 text-[11.5px] font-medium",
    tone === "attention" ? "bg-warn/15 text-foreground" : "bg-selection text-ink-2",
    onClick &&
      "cursor-pointer transition-colors hover:bg-warn/25 focus-visible:outline-2 focus-visible:outline-ring",
    className,
  );
  return onClick ? (
    <button type="button" onClick={onClick} aria-label={label} className={classes}>
      {content}
    </button>
  ) : (
    <span className={classes}>{content}</span>
  );
}

/** Pill-shaped choice or suggestion. */
export function Chip({
  className,
  selected = false,
  ...props
}: ComponentProps<"button"> & { selected?: boolean }) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      className={cn(
        "inline-flex h-8 items-center gap-1.5 rounded-full px-3.5 text-[13px] transition-colors",
        selected
          ? "bg-foreground text-background"
          : "bg-selection text-foreground hover:brightness-95",
        "focus-visible:outline-2 focus-visible:outline-ring disabled:opacity-50",
        className,
      )}
      {...props}
    />
  );
}

/** A suggestion that starts a Conversation: its text, and a small icon when it has one. */
export type Suggestion = string | { text: string; icon?: LucideIcon };

/**
 * A row of quiet suggestion chips (a small icon, then the prompt) that start a Conversation.
 * Shared by `EmptyState` and the empty Conversation's start page.
 */
export function SuggestionChips({
  suggestions,
  onSuggestion,
  className,
}: {
  suggestions: readonly Suggestion[];
  onSuggestion: (text: string) => void;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-wrap justify-center gap-2", className)}>
      {suggestions.map((suggestion) => {
        const { text, icon: Icon } =
          typeof suggestion === "string" ? { text: suggestion, icon: undefined } : suggestion;
        return (
          <Chip
            key={text}
            onClick={() => onSuggestion(text)}
            className="gap-[7px] bg-group text-foreground ring-[0.5px] ring-separator ring-inset hover:bg-selection"
          >
            {Icon ? (
              <Icon size={14} strokeWidth={1.9} aria-hidden="true" className="text-tint" />
            ) : null}
            {text}
          </Chip>
        );
      })}
    </div>
  );
}

/**
 * Label/value rows, e.g. the To / Subject / Body of an email waiting for approval: a
 * compact two-column grid in a subtle muted panel, labels muted in normal case (not mono,
 * not uppercase).
 */
export function DetailRows({ rows }: { rows: { label: ReactNode; value: ReactNode }[] }) {
  return (
    <dl className="grid grid-cols-[76px_1fr] gap-x-3 gap-y-1.5 rounded-xl bg-window p-3 text-[13.5px]">
      {rows.map((row, index) => (
        <div key={index} className="contents">
          <dt className="text-muted-foreground">{row.label}</dt>
          <dd className="min-w-0 break-words text-foreground">{row.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** Thin progress bar (done / total), ink on muted. */
export function Progress({ value, label }: { value: number; label: string }) {
  const clamped = Math.max(0, Math.min(1, value));
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(clamped * 100)}
      className="h-1 w-full overflow-hidden rounded-full bg-selection"
    >
      <div
        className="h-full rounded-full bg-sig-goals transition-[width] duration-300"
        style={{ width: `${clamped * 100}%` }}
      />
    </div>
  );
}

/**
 * The one empty state (the old design notes): centered in the available space, the
 * section's illustration, a plain sans headline, one muted line, and — when
 * `suggestions` and `onSuggestion` are both given — a few chips that start a
 * Conversation with that prompt. Omit them for a quieter inline message (e.g. "nothing
 * matches your search"), which keeps the same centered, breathing layout. Superchat's orb
 * has one home (the sidebar), so an empty state never shows it.
 */
export function EmptyState({
  headline,
  children,
  suggestions,
  onSuggestion,
  illustration,
  className,
}: {
  /** A bundled 3D illustration fitting this section (e.g. a trophy for Goals, books for
   * Library). Leave it out for a quiet inline message. */
  illustration?: IllustrationKey;
  headline?: ReactNode;
  children?: ReactNode;
  suggestions?: readonly string[];
  onSuggestion?: (text: string) => void;
  className?: string;
}) {
  const rich = Boolean(suggestions?.length && onSuggestion);
  return (
    <div
      className={cn(
        "flex flex-1 flex-col items-center justify-center gap-4 px-6 py-16 text-center",
        className,
      )}
    >
      {illustration ? (
        <img src={illustrationUrl(illustration)} alt="" loading="lazy" className="size-14" />
      ) : null}
      <div className="max-w-[380px]">
        {headline ? (
          <p className="text-[17px] font-semibold tracking-[-0.2px] text-foreground">{headline}</p>
        ) : null}
        {children ? (
          <p className={cn("text-[13.5px] text-ink-2", headline ? "mt-1" : undefined)}>
            {children}
          </p>
        ) : null}
      </div>
      {rich && suggestions && onSuggestion ? (
        <SuggestionChips suggestions={suggestions} onSuggestion={onSuggestion} className="pt-1" />
      ) : null}
    </div>
  );
}
