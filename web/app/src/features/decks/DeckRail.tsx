import { useLingui } from "@lingui/react/macro";
import { cn } from "@superchat/ui-web";
import { NavigatorFrame, useNavigatorOrientation } from "../artifacts/editor/NavigatorFrame";
import { DeckThumb } from "./DeckThumb";
import type { DeckSlide } from "./deck-model";

/**
 * The slide rail, the deck's navigator in the editor: a column of same-size 16:9 live
 * thumbnails, each with its number, the current one outlined. Folded, it is a slim strip of
 * slide numbers.
 */
export function DeckRail({
  html,
  slides,
  active,
  onSelect,
}: {
  html: string;
  slides: readonly DeckSlide[];
  active: number;
  onSelect: (index: number) => void;
}) {
  const { t } = useLingui();
  return (
    <NavigatorFrame
      label={t`Slide rail`}
      showLabel={t`Show slides`}
      hideLabel={t`Hide slides`}
      testId="deck-rail"
      folded={<SlideNumbers slides={slides} active={active} onSelect={onSelect} />}
    >
      <div
        role="listbox"
        aria-label={t`Slides`}
        aria-orientation="vertical"
        className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto px-1.5 pt-1 pb-3"
      >
        {slides.map((slide) => (
          <DeckThumb
            key={slide.index}
            html={html}
            index={slide.index}
            label={slide.label}
            active={slide.index === active}
            onSelect={onSelect}
          />
        ))}
      </div>
    </NavigatorFrame>
  );
}

/** The folded rail: one number per slide, a column beside the stage or a strip under it. */
function SlideNumbers({
  slides,
  active,
  onSelect,
}: {
  slides: readonly DeckSlide[];
  active: number;
  onSelect: (index: number) => void;
}) {
  const { t } = useLingui();
  const orientation = useNavigatorOrientation();
  return (
    <div
      role="listbox"
      aria-label={t`Slides`}
      aria-orientation={orientation}
      className={cn(
        "flex min-h-0 flex-1 items-center gap-1",
        orientation === "horizontal"
          ? "flex-row overflow-x-auto overflow-y-hidden px-2 py-1.5"
          : "flex-col overflow-y-auto px-1.5 py-2",
      )}
    >
      {slides.map((slide) => (
        <button
          key={slide.index}
          type="button"
          role="option"
          aria-selected={slide.index === active}
          aria-current={slide.index === active ? "true" : undefined}
          aria-label={slide.label || String(slide.index + 1)}
          title={slide.label.replace(/^\d+\s*/, "") || undefined}
          onClick={() => onSelect(slide.index)}
          className={cn(
            "grid size-8 shrink-0 place-items-center rounded-lg text-[12px] tabular-nums outline-none focus-visible:ring-2 focus-visible:ring-ring",
            slide.index === active
              ? "bg-foreground font-semibold text-background"
              : "text-muted-foreground hover:bg-accent hover:text-foreground",
          )}
        >
          {slide.index + 1}
        </button>
      ))}
    </div>
  );
}
