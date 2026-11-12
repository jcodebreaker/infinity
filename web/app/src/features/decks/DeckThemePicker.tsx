import { useLingui } from "@lingui/react/macro";
import type { Artifact, DeckTheme, HtmlPatch } from "@superchat/contracts";
import { Button, cn } from "@superchat/ui-web";
import { Check, Expand, Undo2, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { type DeckThemesSource, loadDeckThemes, resetDeckThemeCache } from "../../lib/deck-themes";
import { rpc } from "../../lib/rpc";
import { flushEditorEdits, noteOwnVersion } from "../artifacts/editor/editor-state";
import { editConflict } from "../artifacts/editor/html/edit-model";
import type { HtmlEditSource } from "../artifacts/editor/html/useHtmlEditor";
import { DeckThemePreview } from "./DeckThemePreview";
import { setDeckThemeOpen } from "./deck-ui-state";

export type { DeckThemesSource };
/** The id of the theme a saved deck is on, or null when it is hand-made. */
export { resetDeckThemeCache };
export type DeckThemeSource = (artifactId: string) => Promise<string | null>;

const defaultCurrent: DeckThemeSource = async (artifactId) =>
  (await rpc.decks.theme({ artifactId })).theme;
const defaultEdit: HtmlEditSource = (input) => rpc.artifacts.edit(input);

/** The data-attribute the header's Theme button carries: a click on it is not an "outside" click
 * (it toggles the gallery itself) and focus returns to it when the gallery closes. */
export const THEME_BUTTON_ATTR = "data-deck-theme-button";

const CATEGORY_ORDER = ["professional", "editorial", "bold", "dark"] as const;

/**
 * The deck panel's Theme gallery: every theme with its thumbnail, name and mood, the current one
 * marked. Picking one swaps the deck's theme through a `set-theme` source patch (the engine
 * rewrites the kit's theme regions; the slides and the deck's own CSS are not touched), which
 * saves a new `manual` version Superchat is told about like any hand edit. The last switch can be
 * undone here by putting the earlier source back as a newer version.
 *
 * The cards are one radio group with a roving tab stop: Tab enters on the current theme, the
 * arrow keys move between cards, Enter or Space picks.
 */
export function DeckThemePicker({
  deckKey,
  artifact,
  onEdited,
  themesSource,
  themeSource = defaultCurrent,
  editSource = defaultEdit,
  frameRef,
}: {
  deckKey: string;
  artifact: { id: string; version: number };
  onEdited?: (artifactId: string) => void;
  themesSource?: DeckThemesSource;
  themeSource?: DeckThemeSource;
  editSource?: HtmlEditSource;
  /** The deck's frame: only its ready message brings the keyboard back to the gallery. */
  frameRef?: React.RefObject<HTMLIFrameElement | null>;
}) {
  const { t } = useLingui();
  const [themes, setThemes] = useState<DeckTheme[] | null>(null);
  const [failed, setFailed] = useState(false);
  /** The deck's theme: undefined while it is being read, null when it is hand-made. */
  const [current, setCurrent] = useState<string | null | undefined>(undefined);
  const [readSettled, setReadSettled] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [undo, setUndo] = useState<{ prev: string; name: string; version: number } | null>(null);
  const [stop, setStop] = useState(0);
  /** The theme whose sample deck is open in the preview lightbox. */
  const [previewing, setPreviewing] = useState<string | null>(null);
  const previewingRef = useRef<string | null>(null);
  previewingRef.current = previewing;
  const previewOpener = useRef<HTMLElement | null>(null);
  const stopRef = useRef(0);
  stopRef.current = stop;
  const root = useRef<HTMLDivElement>(null);
  const cards = useRef<(HTMLButtonElement | null)[]>([]);
  const focusedOnce = useRef(false);
  const themeSourceRef = useRef(themeSource);
  themeSourceRef.current = themeSource;

  const close = useCallback(
    (returnFocus: boolean) => {
      setDeckThemeOpen(deckKey, false);
      if (returnFocus) {
        window.requestAnimationFrame(() =>
          document.querySelector<HTMLElement>(`[${THEME_BUTTON_ATTR}]`)?.focus(),
        );
      }
    },
    [deckKey],
  );

  useEffect(() => {
    let live = true;
    loadDeckThemes(themesSource).then(
      (found) => live && setThemes(found.themes),
      () => live && setFailed(true),
    );
    return () => {
      live = false;
    };
  }, [themesSource]);

  // The engine says which theme the shown version is on (read again for every new version).
  useEffect(() => {
    let live = true;
    themeSourceRef.current(artifact.id).then(
      (id) => {
        if (!live) return;
        setCurrent(id);
        setReadSettled(true);
      },
      () => {
        if (!live) return;
        setCurrent(undefined);
        setReadSettled(true);
      },
    );
    return () => {
      live = false;
    };
  }, [artifact.id]);

  useEffect(() => {
    // While a theme's preview is open, Escape and clicks belong to the lightbox.
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !previewingRef.current) close(true);
    };
    const onDown = (event: PointerEvent) => {
      const target = event.target as Element | null;
      if (previewingRef.current || target?.closest?.("[data-deck-preview]")) return;
      if (root.current?.contains(target) || target?.closest?.(`[${THEME_BUTTON_ATTR}]`)) return;
      close(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onDown, true);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onDown, true);
    };
  }, [close]);

  // Reading order is the radio order: the sections' cards one after the other.
  const groups = useMemo(
    () =>
      CATEGORY_ORDER.map((category) => ({
        category,
        items: (themes ?? []).filter((theme) => theme.category === category),
      })).filter((group) => group.items.length > 0),
    [themes],
  );
  const order = useMemo(() => groups.flatMap((group) => group.items), [groups]);

  // Opening puts the keyboard on the current theme's card (else the first).
  useEffect(() => {
    if (focusedOnce.current || order.length === 0 || (current === undefined && !readSettled))
      return;
    focusedOnce.current = true;
    const at = Math.max(
      0,
      order.findIndex((theme) => theme.id === current),
    );
    setStop(at);
    cards.current[at]?.focus({ preventScroll: false });
  }, [order, current, readSettled]);

  // The deck reloads in its frame after a switch and the frame takes the keyboard when it reports
  // ready; bring it back to the gallery so Escape and the arrows keep working.
  useEffect(() => {
    const onReady = (event: MessageEvent) => {
      if (!frameRef?.current || event.source !== frameRef.current.contentWindow) return;
      const type = (event.data as { type?: unknown } | null)?.type;
      if (type !== "superchat:deck-ready" && type !== "superchat:edit-ready") return;
      window.requestAnimationFrame(() => {
        if (document.activeElement instanceof HTMLIFrameElement) {
          cards.current[stopRef.current]?.focus({ preventScroll: true });
        }
      });
    };
    window.addEventListener("message", onReady);
    return () => window.removeEventListener("message", onReady);
  }, [frameRef]);

  const categoryLabel: Record<(typeof CATEGORY_ORDER)[number], string> = {
    professional: t`Professional`,
    editorial: t`Editorial`,
    bold: t`Bold`,
    dark: t`Dark`,
  };

  const failure = (cause: unknown): string => {
    const conflict = editConflict(cause);
    if (conflict === "inexact")
      return t`This deck can't be restyled by hand. Ask superchat to restyle it.`;
    if (conflict === "stale") return t`The deck changed since you opened it. Try again.`;
    return t`Couldn't switch the theme.`;
  };

  const send = async (
    patches: HtmlPatch[],
    onDone: (created: Pick<Artifact, "id" | "version">) => void,
  ) => {
    setError(null);
    try {
      // Whatever the editor still has staged is saved first, so the switch builds on it.
      const head = (await flushEditorEdits(deckKey)) ?? artifact;
      const created = await editSource({
        artifactId: head.id,
        baseVersion: head.version,
        patches,
      });
      noteOwnVersion(deckKey, created.version);
      onDone(created);
      onEdited?.(created.id);
    } catch (cause) {
      setError(failure(cause));
    }
  };

  const pick = async (theme: DeckTheme) => {
    if (busy || theme.id === current || current === null) return;
    setBusy(theme.id);
    const prev = current;
    await send([{ kind: "set-theme", theme: theme.id }], (created) => {
      setCurrent(theme.id);
      // Undo is the switch back to the theme the deck was on: it carries nothing else, so it
      // cannot drop an edit made since (a stored source could).
      if (prev) setUndo({ prev, name: theme.name, version: created.version });
    });
    setBusy(null);
  };

  const revert = async () => {
    if (!undo || busy) return;
    setBusy("undo");
    await send([{ kind: "set-theme", theme: undo.prev }], () => {
      setCurrent(undefined);
      setUndo(null);
    });
    setBusy(null);
  };

  const move = (event: React.KeyboardEvent, index: number) => {
    const last = order.length - 1;
    const next =
      event.key === "ArrowRight" || event.key === "ArrowDown"
        ? Math.min(last, index + 1)
        : event.key === "ArrowLeft" || event.key === "ArrowUp"
          ? Math.max(0, index - 1)
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? last
              : null;
    if (next === null) return;
    event.preventDefault();
    setStop(next);
    cards.current[next]?.focus();
  };

  const canUndo = undo !== null && undo.version === artifact.version;
  let position = -1;

  return (
    <>
      {/* A click anywhere over the stage (a frame never reports clicks to the page) dismisses. */}
      <div
        aria-hidden="true"
        data-testid="deck-theme-backdrop"
        className="absolute inset-0 z-[25]"
        onPointerDown={() => close(false)}
      />
      <div
        ref={root}
        role="dialog"
        aria-label={t`Theme`}
        data-testid="deck-theme-picker"
        className="absolute end-3 top-3 z-30 flex max-h-[calc(100%-1.5rem)] w-[min(440px,calc(100%-1.5rem))] flex-col overflow-hidden rounded-2xl bg-popover text-popover-foreground shadow-[0_12px_40px_rgb(0_0_0/0.16)] ring-1 ring-border motion-safe:animate-[deck-inspector-in_180ms_ease-out]"
      >
        <header className="flex items-center gap-1 px-4 py-3">
          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-medium">{t`Theme`}</div>
            <div className="truncate text-[11px] text-muted-foreground">
              {current === null
                ? t`This deck isn't on a theme. Ask superchat to restyle it.`
                : t`Restyles every slide; your content stays as it is.`}
            </div>
          </div>
          <Button
            variant="ghost"
            size="icon-sm"
            className="text-muted-foreground"
            aria-label={t`Close`}
            title={t`Close`}
            onClick={() => close(true)}
          >
            <X />
          </Button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 pb-3">
          {failed ? (
            <p className="py-6 text-center text-[12px] text-muted-foreground">
              {t`Couldn't load the themes. Try again in a moment.`}
            </p>
          ) : themes === null ? (
            <div className="grid grid-cols-2 gap-3" aria-busy="true">
              {[0, 1, 2, 3].map((n) => (
                <div
                  key={n}
                  className="aspect-video animate-pulse rounded-lg bg-muted motion-reduce:animate-none"
                />
              ))}
            </div>
          ) : (
            <div role="radiogroup" aria-label={t`Theme`}>
              {groups.map((group) => (
                <section key={group.category} className="mb-4 last:mb-1">
                  <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                    {categoryLabel[group.category]}
                  </h3>
                  <div className="grid grid-cols-2 gap-3">
                    {group.items.map((theme) => {
                      position += 1;
                      const index = position;
                      const selected = theme.id === current;
                      return (
                        <div key={theme.id} className="group/tile relative min-w-0">
                          {/* biome-ignore lint/a11y/useSemanticElements: a styled card, not a form radio */}
                          <button
                            ref={(node) => {
                              cards.current[index] = node;
                            }}
                            type="button"
                            role="radio"
                            aria-checked={selected}
                            aria-disabled={current === null || busy !== null || undefined}
                            tabIndex={index === stop ? 0 : -1}
                            data-testid={`deck-theme-${theme.id}`}
                            onClick={() => void pick(theme)}
                            onFocus={() => setStop(index)}
                            onKeyDown={(event) => move(event, index)}
                            className={cn(
                              "group flex min-w-0 flex-col gap-1.5 rounded-xl text-start outline-none focus-visible:ring-2 focus-visible:ring-ring",
                              busy !== null && "opacity-60",
                              current === null && "opacity-60",
                            )}
                          >
                            <span
                              className={cn(
                                "relative block aspect-video overflow-hidden rounded-lg bg-muted outline -outline-offset-1 motion-safe:transition-[outline-color]",
                                selected
                                  ? "outline-2 -outline-offset-2 outline-foreground"
                                  : "outline-1 outline-border group-hover:outline-foreground/40",
                              )}
                            >
                              {theme.preview ? (
                                <img
                                  src={theme.preview}
                                  alt=""
                                  draggable={false}
                                  className="size-full object-cover"
                                />
                              ) : null}
                              {selected ? (
                                <span className="absolute start-1.5 top-1.5 grid size-5 place-items-center rounded-full bg-foreground text-background shadow">
                                  <Check size={12} />
                                </span>
                              ) : null}
                            </span>
                            <span className="px-0.5">
                              <span className="block truncate text-[12px] font-medium">
                                {theme.name}
                              </span>
                              <span className="block truncate text-[11px] leading-snug text-muted-foreground">
                                {theme.tagline}
                              </span>
                            </span>
                          </button>
                          <button
                            type="button"
                            tabIndex={index === stop ? 0 : -1}
                            aria-label={t`Preview ${theme.name}`}
                            title={t`Preview`}
                            data-testid={`deck-theme-preview-${theme.id}`}
                            onClick={(event) => {
                              previewOpener.current = event.currentTarget;
                              setPreviewing(theme.id);
                            }}
                            className="absolute top-1.5 right-1.5 grid size-6 place-items-center rounded-full bg-background/90 text-foreground opacity-0 shadow-sm ring-1 ring-border outline-none backdrop-blur-sm transition-opacity group-hover/tile:opacity-100 focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-ring [@media(hover:none)]:opacity-100"
                          >
                            <Expand size={12} aria-hidden="true" />
                          </button>
                        </div>
                      );
                    })}
                  </div>
                </section>
              ))}
            </div>
          )}
        </div>
        {error || canUndo ? (
          <footer
            role="status"
            className="flex items-center gap-2 border-t border-border px-4 py-2 text-[12px]"
          >
            <span className="min-w-0 flex-1 truncate">
              {error ?? t`Switched to ${undo?.name ?? ""}`}
            </span>
            {canUndo ? (
              <Button
                variant="ghost"
                size="sm"
                disabled={busy !== null}
                onClick={() => void revert()}
              >
                <Undo2 />
                {t`Undo`}
              </Button>
            ) : null}
          </footer>
        ) : null}
      </div>
      <DeckThemePreview
        themes={order}
        themeId={previewing}
        onThemeChange={setPreviewing}
        onOpenChange={(open) => !open && setPreviewing(null)}
        finalFocus={previewOpener}
        current={current}
        onUse={
          current === null
            ? undefined
            : (theme) => {
                setPreviewing(null);
                void pick(theme);
              }
        }
      />
    </>
  );
}
