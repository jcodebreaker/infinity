import { useLingui } from "@lingui/react/macro";
import type { DeckTheme } from "@superchat/contracts";
import {
  Button,
  cn,
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogTitle,
  Skeleton,
} from "@superchat/ui-web";
import { Check, ChevronLeft, ChevronRight, X } from "lucide-react";
import { type RefObject, useEffect, useState } from "react";
import { loadDeckThemeSample } from "../../lib/deck-themes";
import { DeckPreview } from "./DeckViewer";

/** At most this many themes show as one row of names; more flip with arrows. */
const NAMED_SWITCH_MAX = 5;

/**
 * A theme's whole sample deck (cover, agenda, figures, chart, table, quote, close) in the deck
 * viewer, in a lightbox: the slides, prev/next, the counter and the arrow keys are the viewer's
 * own. The person flips between `themes` from the header; "Use this look" chooses the shown one
 * (an ask card's answer, or the Theme gallery's switch).
 */
export function DeckThemePreview({
  themes,
  themeId,
  onThemeChange,
  onOpenChange,
  onUse,
  current,
  finalFocus,
}: {
  /** The themes to flip between, in order. */
  themes: readonly DeckTheme[];
  /** The shown theme; null closes the lightbox. */
  themeId: string | null;
  onThemeChange: (themeId: string) => void;
  onOpenChange: (open: boolean) => void;
  /** Chooses the shown theme; without it the lightbox only shows. */
  onUse?: (theme: DeckTheme) => void;
  /** The theme already in use (its button reads as chosen). */
  current?: string | null;
  finalFocus?: RefObject<HTMLElement | null>;
}) {
  const { t } = useLingui();
  const index = themes.findIndex((theme) => theme.id === themeId);
  const theme = index >= 0 ? themes[index] : undefined;
  const [sample, setSample] = useState<{ id: string; html: string } | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  useEffect(() => {
    if (!themeId) return;
    let live = true;
    setFailed(null);
    loadDeckThemeSample(themeId).then(
      (html) => live && setSample({ id: themeId, html }),
      () => live && setFailed(themeId),
    );
    return () => {
      live = false;
    };
  }, [themeId]);

  const flip = (step: number) => {
    const next = themes[(index + step + themes.length) % themes.length];
    if (next) onThemeChange(next.id);
  };
  const ready = sample !== null && sample.id === themeId;
  const named = themes.length <= NAMED_SWITCH_MAX;

  return (
    <Dialog open={!!theme} onOpenChange={onOpenChange}>
      <DialogContent
        showCloseButton={false}
        finalFocus={finalFocus}
        data-deck-preview=""
        className="flex h-[min(88dvh,860px)] w-[min(1180px,calc(100vw-1.5rem))] flex-col gap-0 overflow-hidden rounded-2xl p-0 sm:max-w-none"
      >
        <header className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-2 border-b border-border px-4 py-3">
          <div className="min-w-0 flex-1">
            <DialogTitle className="truncate text-[15px] font-semibold">{theme?.name}</DialogTitle>
            <DialogDescription className="truncate text-[12.5px] text-muted-foreground">
              {theme?.tagline}
            </DialogDescription>
          </div>
          {themes.length > 1 ? (
            named ? (
              <div className="order-last flex w-full gap-1 overflow-x-auto rounded-full bg-muted p-1 sm:order-none sm:w-auto">
                {themes.map((option) => (
                  <button
                    key={option.id}
                    type="button"
                    aria-pressed={option.id === themeId}
                    onClick={() => onThemeChange(option.id)}
                    className={cn(
                      "h-7 shrink-0 rounded-full px-3 text-[12.5px] font-medium whitespace-nowrap text-muted-foreground outline-none transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring",
                      option.id === themeId && "bg-background text-foreground shadow-sm",
                    )}
                  >
                    {option.name}
                  </button>
                ))}
              </div>
            ) : (
              <div className="flex items-center gap-1">
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={t`Previous look`}
                  onClick={() => flip(-1)}
                >
                  <ChevronLeft />
                </Button>
                <span className="min-w-12 text-center text-[12.5px] text-muted-foreground tabular-nums">
                  {index + 1} / {themes.length}
                </span>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={t`Next look`}
                  onClick={() => flip(1)}
                >
                  <ChevronRight />
                </Button>
              </div>
            )
          ) : null}
          <div className="flex items-center gap-1.5">
            {theme && theme.id === current ? (
              <Button size="sm" variant="secondary" disabled className="disabled:opacity-100">
                <Check />
                {t`Current look`}
              </Button>
            ) : theme && onUse ? (
              <Button size="sm" onClick={() => onUse(theme)}>
                {t`Use this look`}
              </Button>
            ) : null}
            <DialogClose
              render={
                <Button
                  variant="ghost"
                  size="icon-sm"
                  className="text-muted-foreground"
                  aria-label={t`Close`}
                />
              }
            >
              <X />
            </DialogClose>
          </div>
        </header>
        <div className="min-h-0 flex-1">
          {ready && theme ? (
            <DeckPreview key={theme.id} html={sample.html} title={`${theme.id}.sample.deck.html`} />
          ) : failed === themeId ? (
            <p className="grid h-full place-items-center text-[13px] text-muted-foreground">
              {t`Couldn't load this look. Try again in a moment.`}
            </p>
          ) : (
            <div className="flex h-full gap-4 p-4" aria-busy="true">
              <div className="hidden w-40 shrink-0 flex-col gap-3 md:flex">
                {[0, 1, 2, 3].map((n) => (
                  <Skeleton key={n} className="aspect-video w-full rounded-md" />
                ))}
              </div>
              <Skeleton className="m-auto aspect-video w-full max-w-4xl rounded-lg" />
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
