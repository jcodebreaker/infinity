import { useLingui } from "@lingui/react/macro";
import type { DeckTheme } from "@superchat/contracts";
import { cn } from "@superchat/ui-web";
import { Check, Expand } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import type { AskPreviewProps } from "../../components/cards/context";
import { useDeckThemes } from "../../lib/deck-themes";
import { DeckThemePreview } from "./DeckThemePreview";

/**
 * The look question's answers as theme tiles: each theme's thumbnail, its name and its few-word
 * tagline, and a Preview that opens the theme's whole sample deck. Picking a tile (or "Use this
 * look" in the preview) answers once; then the chosen tile carries a check, the others dim and
 * the card is locked. Three tiles fill the card side by side; a narrow card lists them.
 */
export function DeckThemeChoice({ options, chosen, locked, onPick }: AskPreviewProps) {
  const { t } = useLingui();
  const themes = useDeckThemes();
  const [previewing, setPreviewing] = useState<string | null>(null);
  const opener = useRef<HTMLElement | null>(null);
  const byId = useMemo(() => new Map((themes ?? []).map((theme) => [theme.id, theme])), [themes]);
  const offered = useMemo(
    () => options.map((option) => byId.get(option.previewId)).filter((x): x is DeckTheme => !!x),
    [options, byId],
  );

  return (
    <div className="@container">
      <ul
        className="mt-3 grid grid-cols-1 gap-2.5 @[28rem]:grid-cols-3 @[28rem]:gap-3"
        data-testid="ask-previews"
      >
        {options.map((option) => {
          const theme = byId.get(option.previewId);
          const isChosen = chosen === option.label;
          return (
            <li
              key={option.id}
              className={cn(
                "group/tile relative min-w-0 motion-safe:transition-opacity",
                locked && !isChosen && "opacity-45",
              )}
            >
              <button
                type="button"
                disabled={locked}
                aria-pressed={locked ? isChosen : undefined}
                onClick={() => onPick(option.label)}
                className="peer/pick flex w-full min-w-0 items-center gap-3 rounded-xl text-start outline-none enabled:cursor-pointer @[28rem]:flex-col @[28rem]:items-stretch @[28rem]:gap-2"
              >
                <span className="relative block aspect-video w-[46%] shrink-0 overflow-hidden rounded-[10px] bg-muted @[28rem]:w-full">
                  {theme?.preview ? (
                    <img
                      src={theme.preview}
                      alt=""
                      draggable={false}
                      className="size-full object-cover"
                    />
                  ) : null}
                </span>
                <span className="min-w-0 flex-1 px-0.5">
                  <span className="block text-[13px] leading-tight font-medium" dir="auto">
                    {option.label}
                  </span>
                  {theme ? (
                    <span className="mt-0.5 block text-[12px] leading-snug text-muted-foreground">
                      {theme.tagline}
                    </span>
                  ) : null}
                </span>
              </button>
              {/* Over the picture, mirroring its box: the ring (so the image can never cover
                  it), the chosen check and the Preview button. */}
              <div
                className={cn(
                  "pointer-events-none absolute top-0 left-0 aspect-video w-[46%] rounded-[10px] ring-1 ring-border ring-offset-card motion-safe:transition-shadow @[28rem]:w-full",
                  // rings sit outside the picture, so they show on light and dark tiles alike
                  isChosen
                    ? "ring-2 ring-foreground ring-offset-2"
                    : [
                        !locked &&
                          "group-hover/tile:ring-2 group-hover/tile:ring-foreground/40 group-hover/tile:ring-offset-2",
                        "peer-focus-visible/pick:ring-2 peer-focus-visible/pick:ring-ring peer-focus-visible/pick:ring-offset-2",
                      ],
                )}
              >
                {isChosen ? (
                  <span className="absolute right-2 bottom-2 grid size-5 place-items-center rounded-full bg-foreground text-background shadow-sm">
                    <Check size={12} strokeWidth={3} aria-hidden="true" />
                  </span>
                ) : null}
                {theme ? (
                  <button
                    type="button"
                    aria-label={t`Preview ${theme.name}`}
                    title={t`Preview`}
                    onClick={(event) => {
                      opener.current = event.currentTarget;
                      setPreviewing(theme.id);
                    }}
                    className="pointer-events-auto absolute top-1.5 right-1.5 grid size-7 place-items-center rounded-full bg-background/90 text-foreground opacity-0 shadow-sm ring-1 ring-border outline-none backdrop-blur-sm transition-opacity group-hover/tile:opacity-100 focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-ring [@media(hover:none)]:opacity-100"
                  >
                    <Expand size={13} aria-hidden="true" />
                  </button>
                ) : null}
              </div>
            </li>
          );
        })}
      </ul>
      <DeckThemePreview
        themes={offered}
        themeId={previewing}
        onThemeChange={setPreviewing}
        onOpenChange={(open) => !open && setPreviewing(null)}
        finalFocus={opener}
        current={locked ? (options.find((o) => o.label === chosen)?.previewId ?? null) : undefined}
        onUse={
          locked
            ? undefined
            : (theme) => {
                const option = options.find((o) => o.previewId === theme.id);
                setPreviewing(null);
                if (option) onPick(option.label);
              }
        }
      />
    </div>
  );
}
