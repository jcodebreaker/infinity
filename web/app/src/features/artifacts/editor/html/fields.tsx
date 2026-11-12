import { Button, cn, Input } from "@superchat/ui-web";
import { type ReactNode, useId, useState } from "react";

// Small controls of the style panel. They are controlled by the selection (so they always show
// what the element has) and report changes as they happen ("later": a drag or keystroke, saved
// after a quiet moment) and when the person is done ("now": release, Enter, blur).

export type ChangeMode = "later" | "now";

export function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[84px_1fr] items-center gap-2 text-[12px]">
      <span className="truncate text-muted-foreground">{label}</span>
      <div className="flex min-w-0 items-center gap-1.5">{children}</div>
    </div>
  );
}

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2.5 border-t border-border px-4 py-3.5 first:border-t-0">
      <h3 className="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
        {title}
      </h3>
      {children}
    </section>
  );
}

/** A number with a unit shown after it. Typing saves later; Enter or leaving the field saves now. */
export function NumberField({
  value,
  onChange,
  label,
  unit,
  min,
  max,
  step = 1,
  className,
}: {
  value: number | null;
  onChange: (value: number, mode: ChangeMode) => void;
  label: string;
  unit?: string;
  min?: number;
  max?: number;
  step?: number;
  className?: string;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const shown = draft ?? (value === null ? "" : String(Math.round(value * 100) / 100));
  const clamp = (n: number) => Math.min(max ?? n, Math.max(min ?? n, n));
  const commit = () => {
    if (draft === null) return;
    const n = Number(draft);
    if (draft.trim() !== "" && Number.isFinite(n)) onChange(clamp(n), "now");
    setDraft(null);
  };
  return (
    <div className={cn("relative min-w-0 flex-1", className)}>
      <Input
        type="text"
        inputMode="decimal"
        aria-label={label}
        value={shown}
        onChange={(event) => {
          setDraft(event.target.value);
          const n = Number(event.target.value);
          if (event.target.value.trim() !== "" && Number.isFinite(n)) onChange(clamp(n), "later");
        }}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            commit();
          } else if (event.key === "ArrowUp" || event.key === "ArrowDown") {
            event.preventDefault();
            const dir = event.key === "ArrowUp" ? 1 : -1;
            const next = clamp((Number(shown) || 0) + dir * step * (event.shiftKey ? 10 : 1));
            setDraft(null);
            onChange(next, "later");
          }
        }}
        onBlur={commit}
        className="h-7 pe-6 text-[12px] tabular-nums"
      />
      {unit ? (
        <span className="pointer-events-none absolute end-2 top-1/2 -translate-y-1/2 text-[11px] text-muted-foreground">
          {unit}
        </span>
      ) : null}
    </div>
  );
}

/** A range slider with its number. Dragging saves once, on release. */
export function SliderField({
  value,
  onChange,
  label,
  min,
  max,
  step = 1,
  unit,
}: {
  value: number;
  onChange: (value: number, mode: ChangeMode) => void;
  label: string;
  min: number;
  max: number;
  step?: number;
  unit?: string;
}) {
  const id = useId();
  const [drag, setDrag] = useState<number | null>(null);
  const shown = drag ?? value;
  const done = () => {
    if (drag !== null) onChange(drag, "now");
    setDrag(null);
  };
  return (
    <>
      <input
        id={id}
        type="range"
        aria-label={label}
        min={min}
        max={max}
        step={step}
        value={Math.min(max, Math.max(min, shown))}
        onChange={(event) => {
          const n = Number(event.target.value);
          setDrag(n);
          onChange(n, "later");
        }}
        onPointerUp={done}
        onKeyUp={done}
        onBlur={done}
        className="h-1.5 min-w-0 flex-1 cursor-pointer accent-foreground"
      />
      <NumberField
        value={shown}
        onChange={onChange}
        label={`${label} value`}
        unit={unit}
        min={min}
        max={max}
        step={step}
        className="w-[68px] flex-none"
      />
    </>
  );
}

export type Swatch = { name: string; value: string; css: string };

/** Theme colours as swatches (they write `var(--token)`, so the deck stays on its palette), plus
 * a custom colour; `allowNone` adds a "no fill" choice. */
export function ColorField({
  swatches,
  current,
  currentCss,
  onPick,
  label,
  noneLabel,
  customLabel,
}: {
  swatches: readonly Swatch[];
  /** The element's colour as #rrggbb ("" for none). */
  current: string;
  /** What the element authored (`var(--accent)`), to mark the token. */
  currentCss?: string;
  onPick: (css: string, mode: ChangeMode) => void;
  label: string;
  noneLabel?: string;
  customLabel: string;
}) {
  return (
    <fieldset
      aria-label={label}
      className="m-0 flex min-w-0 flex-wrap items-center gap-1.5 border-0 p-0"
    >
      {noneLabel ? (
        <button
          type="button"
          aria-label={noneLabel}
          aria-pressed={current === ""}
          title={noneLabel}
          onClick={() => onPick("transparent", "now")}
          className={cn(
            "size-5 rounded-full border border-border bg-[linear-gradient(135deg,transparent_46%,var(--destructive)_46%,var(--destructive)_54%,transparent_54%)]",
            current === "" && "ring-2 ring-foreground ring-offset-1 ring-offset-background",
          )}
        />
      ) : null}
      {swatches.map((swatch) => {
        const on = currentCss
          ? currentCss.replace(/\s/g, "") === swatch.css.replace(/\s/g, "")
          : current === swatch.value;
        return (
          <button
            key={swatch.name}
            type="button"
            aria-label={swatch.name.replace(/^--/, "")}
            aria-pressed={on}
            title={swatch.name.replace(/^--/, "")}
            onClick={() => onPick(swatch.css, "now")}
            style={{ background: swatch.value }}
            className={cn(
              "size-5 rounded-full border border-border",
              on && "ring-2 ring-foreground ring-offset-1 ring-offset-background",
            )}
          />
        );
      })}
      <label
        title={customLabel}
        className="relative grid size-5 cursor-pointer place-items-center overflow-hidden rounded-full border border-border bg-[conic-gradient(red,yellow,lime,aqua,blue,magenta,red)]"
      >
        <input
          type="color"
          aria-label={customLabel}
          value={/^#[0-9a-f]{6}$/i.test(current) ? current : "#000000"}
          onChange={(event) => onPick(event.target.value, "later")}
          onBlur={(event) => onPick(event.target.value, "now")}
          className="absolute inset-0 size-full cursor-pointer opacity-0"
        />
      </label>
    </fieldset>
  );
}

export function IconToggle({
  pressed,
  onPressedChange,
  label,
  children,
}: {
  pressed: boolean;
  onPressedChange: (pressed: boolean) => void;
  label: string;
  children: ReactNode;
}) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-sm"
      aria-label={label}
      title={label}
      aria-pressed={pressed}
      onClick={() => onPressedChange(!pressed)}
      className={cn("size-7", pressed && "bg-muted text-foreground")}
    >
      {children}
    </Button>
  );
}
