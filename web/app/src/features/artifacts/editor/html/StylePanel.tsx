// Portions modified from nexu-io/open-design apps/web/src/components/ManualEditPanel.tsx@802708f, Apache-2.0; changes: rewritten small with shadcn components; fonts and colours limited to the deck's theme tokens, controls report preview-now / save-later changes, asking superchat lives in the ask box under the stage.
import { useLingui } from "@lingui/react/macro";
import type { DeckEditTarget, DeckEditTheme } from "@superchat/contracts";
import { Button, Input, NativeSelect, NativeSelectOption } from "@superchat/ui-web";
import { AlignCenter, AlignLeft, AlignRight, Copy, Italic, Trash2, X } from "lucide-react";
import { useState } from "react";
import {
  firstFamily,
  isPathId,
  num,
  px,
  readTranslate,
  type StyleChanges,
  targetLabel,
  toHex,
  withTranslate,
} from "./edit-model";
import {
  type ChangeMode,
  ColorField,
  IconToggle,
  NumberField,
  Row,
  Section,
  SliderField,
} from "./fields";
import type { EditNotice } from "./useHtmlEditor";

const WEIGHTS = [300, 400, 500, 600, 700, 800] as const;

export type StylePanelProps = {
  targets: readonly DeckEditTarget[];
  theme: DeckEditTheme | null;
  slideCount: number;
  onStyle: (changes: StyleChanges, mode: ChangeMode) => void;
  onAttributes: (attributes: { href?: string | null; alt?: string | null }) => void;
  onCommit: () => void;
  /** Closes the inspector; the selection stays. */
  onClose: () => void;
  onRemove: () => void;
  onDuplicate: () => void;
  /** Labels without slide numbers (a page). */
  inDeck?: boolean;
  /** Focuses the ask box (offered for what cannot be changed by hand). */
  onAsk?: () => void;
};

/**
 * The compact inspector of the HTML editor (a deck, a page): what the selection looks like and
 * controls that change it. Fonts and colours come from the file's own theme tokens, so a change
 * stays on its palette; everything else is plain CSS the engine writes into the element's inline
 * style. An element with no stable id (a page element the bridge addresses by its place) cannot be
 * changed by hand; the inspector says so and offers superchat.
 */
export function StylePanel(props: StylePanelProps) {
  const { t } = useLingui();
  const { targets } = props;
  const primary = targets[targets.length - 1];
  const words = {
    slide: t`Slide`,
    heading: t`Heading`,
    text: t`Text`,
    image: t`Image`,
    link: t`Link`,
    box: t`Box`,
  };
  const isSlide = targets.length > 0 && targets.every((target) => target.kind === "slide");
  const hasText = targets.some((target) => target.kind === "text" || target.kind === "link");
  if (!primary) return <EmptyInspector theme={props.theme} onClose={props.onClose} />;
  const title =
    targets.length > 1
      ? t`${targets.length} elements`
      : targetLabel(primary, words, props.inDeck ?? true);
  const unstable = targets.some((target) => isPathId(target.id));

  return (
    <aside
      data-testid="deck-style-panel"
      aria-label={t`Edit`}
      onPointerDownCapture={props.onCommit}
      className="flex max-h-full w-[288px] flex-col overflow-y-auto overscroll-contain rounded-2xl bg-popover text-popover-foreground shadow-[0_12px_40px_rgb(0_0_0/0.16)] ring-1 ring-border motion-safe:animate-[deck-inspector-in_180ms_ease-out]"
    >
      <header className="flex items-center gap-1 px-4 py-3">
        <div className="min-w-0 flex-1">
          <div className="truncate text-[13px] font-medium" dir="auto">
            {title}
          </div>
          {targets.length === 1 ? (
            <div className="truncate text-[11px] text-muted-foreground">
              {isPathId(primary.id) ? (primary.where ?? "") : primary.id}
            </div>
          ) : null}
        </div>
        <CloseInspector onClose={props.onClose} />
      </header>
      {unstable ? (
        <div data-testid="style-panel-unstable" className="flex flex-col gap-2.5 px-4 pb-4">
          <p className="text-[12px] text-muted-foreground">
            {t`This part of the page has no stable name in the file, so it can't be changed by hand.`}
          </p>
          {props.onAsk ? (
            <Button size="sm" variant="outline" onClick={props.onAsk}>
              {t`Ask superchat instead`}
            </Button>
          ) : null}
        </div>
      ) : (
        <StyleControls {...props} primary={primary} isSlide={isSlide} hasText={hasText} />
      )}
    </aside>
  );
}

function StyleControls(
  props: StylePanelProps & { primary: DeckEditTarget; isSlide: boolean; hasText: boolean },
) {
  const { t } = useLingui();
  const { primary, isSlide, hasText } = props;
  return (
    <>
      <div className="flex items-center gap-1 px-4 pb-3">
        <Button variant="ghost" size="sm" onClick={props.onDuplicate}>
          <Copy />
          {t`Duplicate`}
        </Button>
        <Button
          variant="ghost"
          size="sm"
          disabled={isSlide && props.slideCount <= 1}
          onClick={props.onRemove}
        >
          <Trash2 />
          {t`Delete`}
        </Button>
      </div>
      {hasText ? <TextSection {...props} primary={primary} /> : null}
      <BoxSection {...props} primary={primary} isSlide={isSlide} />
      {isSlide ? null : <PositionSection {...props} primary={primary} />}
      {primary.kind === "link" || primary.kind === "image" ? (
        <AttributeSection key={primary.id} {...props} primary={primary} />
      ) : null}
    </>
  );
}

function CloseInspector({ onClose }: { onClose: () => void }) {
  const { t } = useLingui();
  return (
    <Button
      variant="ghost"
      size="icon-sm"
      className="text-muted-foreground"
      aria-label={t`Close`}
      title={t`Close (Esc)`}
      onClick={onClose}
    >
      <X />
    </Button>
  );
}

/** The inspector opened with nothing selected: what to do, and the file's palette to see. */
function EmptyInspector({ theme, onClose }: { theme: DeckEditTheme | null; onClose: () => void }) {
  const { t } = useLingui();
  const swatches = COLOR_SWATCHES(theme);
  return (
    <aside
      data-testid="deck-style-empty"
      aria-label={t`Style`}
      className="flex max-h-full w-[288px] flex-col gap-4 overflow-y-auto rounded-2xl bg-popover px-4 py-4 text-popover-foreground ring-1 ring-border"
    >
      <div className="-me-2 -mt-1 flex items-center gap-1">
        <p className="min-w-0 flex-1 text-[12px] text-muted-foreground">
          {t`Click an element to edit it.`}
        </p>
        <CloseInspector onClose={onClose} />
      </div>
      {swatches.length ? (
        <div>
          <div className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
            {t`Palette`}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {swatches.map((swatch) => (
              <span
                key={swatch.name}
                title={swatch.name.replace(/^--/, "")}
                role="img"
                aria-label={swatch.name.replace(/^--/, "")}
                className="size-5 rounded-full ring-1 ring-border ring-inset"
                style={{ background: swatch.value }}
              />
            ))}
          </div>
        </div>
      ) : null}
    </aside>
  );
}

/** The save / stale notice, shown over the stage whether or not anything is selected. */
export function EditNoticeBanner({
  notice,
  onDismissNotice,
  onAsk,
  canAsk,
}: {
  notice: EditNotice | null;
  onDismissNotice: () => void;
  /** Focuses the ask box. */
  onAsk: () => void;
  canAsk: boolean;
}) {
  const { t } = useLingui();
  if (!notice) return null;
  const text =
    notice.kind === "inexact"
      ? t`That change can't be made exactly by hand.`
      : notice.kind === "stale"
        ? t`This isn't the latest version. Open the latest to edit it.`
        : t`Couldn't save that change.`;
  return (
    <div
      role="status"
      data-testid="deck-edit-notice"
      className="flex w-fit max-w-[360px] flex-col gap-2 rounded-2xl bg-popover px-3.5 py-2.5 text-[12px] text-popover-foreground shadow-[0_8px_32px_rgb(0_0_0/0.14)] ring-1 ring-border"
    >
      <div className="flex items-start gap-2">
        <span className="flex-1">{text}</span>
        <button
          type="button"
          aria-label={t`Dismiss`}
          onClick={onDismissNotice}
          className="text-muted-foreground hover:text-foreground"
        >
          <X size={13} />
        </button>
      </div>
      {notice.kind === "inexact" && canAsk ? (
        <Button size="sm" variant="outline" onClick={onAsk}>
          {t`Ask superchat instead`}
        </Button>
      ) : null}
    </div>
  );
}

const COLOR_SWATCHES = (theme: DeckEditTheme | null) =>
  (theme?.colors ?? []).map((c) => ({
    name: c.name,
    value: toHex(c.value) || c.value,
    css: `var(${c.name})`,
  }));

function authored(target: DeckEditTarget, prop: string): string | undefined {
  return target.inline[prop];
}

function TextSection({ primary, theme, onStyle }: StylePanelProps & { primary: DeckEditTarget }) {
  const { t } = useLingui();
  const c = primary.computed;
  const family = firstFamily(c["font-family"]);
  const fonts = theme?.fonts ?? [];
  const matched = fonts.find((f) => f.label === family);
  const size = px(c["font-size"]) ?? 16;
  const lineHeight = px(c["line-height"]);
  const ratio = lineHeight === null ? 1.2 : lineHeight / size;
  const weight = Number(c["font-weight"]) || 400;
  const align = c["text-align"] === "start" ? "left" : c["text-align"];
  return (
    <Section title={t`Text`}>
      <Row label={t`Font`}>
        <NativeSelect
          size="sm"
          className="w-full"
          aria-label={t`Font`}
          value={matched ? `var(${matched.name})` : ""}
          onChange={(event) =>
            event.target.value && onStyle({ "font-family": event.target.value }, "now")
          }
        >
          {matched ? null : <NativeSelectOption value="">{family || t`Custom`}</NativeSelectOption>}
          {fonts.map((font) => (
            <NativeSelectOption key={font.name} value={`var(${font.name})`}>
              {font.label}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      </Row>
      <Row label={t`Size`}>
        <NumberField
          label={t`Font size`}
          unit="px"
          value={size}
          min={8}
          max={400}
          onChange={(n, mode) => onStyle({ "font-size": `${num(n)}px` }, mode)}
        />
        <NativeSelect
          size="sm"
          aria-label={t`Weight`}
          value={String(weight)}
          onChange={(event) => onStyle({ "font-weight": event.target.value }, "now")}
        >
          {WEIGHTS.map((w) => (
            <NativeSelectOption key={w} value={String(w)}>
              {w}
            </NativeSelectOption>
          ))}
          {WEIGHTS.includes(weight as (typeof WEIGHTS)[number]) ? null : (
            <NativeSelectOption value={String(weight)}>{weight}</NativeSelectOption>
          )}
        </NativeSelect>
      </Row>
      <Row label={t`Style`}>
        <IconToggle
          label={t`Italic`}
          pressed={c["font-style"] === "italic"}
          onPressedChange={(on) => onStyle({ "font-style": on ? "italic" : "normal" }, "now")}
        >
          <Italic />
        </IconToggle>
        <span className="mx-1 h-4 w-px bg-border" />
        {(
          [
            ["left", t`Align left`, <AlignLeft key="l" />],
            ["center", t`Align center`, <AlignCenter key="c" />],
            ["right", t`Align right`, <AlignRight key="r" />],
          ] as const
        ).map(([value, label, icon]) => (
          <IconToggle
            key={value}
            label={label}
            pressed={align === value}
            onPressedChange={() => onStyle({ "text-align": value }, "now")}
          >
            {icon}
          </IconToggle>
        ))}
      </Row>
      <Row label={t`Color`}>
        <ColorField
          label={t`Text color`}
          swatches={COLOR_SWATCHES(theme)}
          current={toHex(c.color)}
          currentCss={authored(primary, "color")}
          customLabel={t`Custom color`}
          onPick={(css, mode) => onStyle({ color: css }, mode)}
        />
      </Row>
      <Row label={t`Line height`}>
        <SliderField
          label={t`Line height`}
          min={0.8}
          max={2.5}
          step={0.05}
          value={Math.round(ratio * 100) / 100}
          onChange={(n, mode) => onStyle({ "line-height": String(num(n)) }, mode)}
        />
      </Row>
      <Row label={t`Spacing`}>
        <SliderField
          label={t`Letter spacing`}
          unit="px"
          min={-10}
          max={40}
          step={0.5}
          value={px(c["letter-spacing"]) ?? 0}
          onChange={(n, mode) => onStyle({ "letter-spacing": `${num(n)}px` }, mode)}
        />
      </Row>
    </Section>
  );
}

function BoxSection({
  primary,
  theme,
  onStyle,
  isSlide,
}: StylePanelProps & { primary: DeckEditTarget; isSlide: boolean }) {
  const { t } = useLingui();
  const c = primary.computed;
  return (
    <Section title={isSlide ? t`Background` : t`Box`}>
      <Row label={t`Fill`}>
        <ColorField
          label={t`Fill color`}
          swatches={COLOR_SWATCHES(theme)}
          current={toHex(c["background-color"])}
          currentCss={authored(primary, "background-color")}
          noneLabel={t`No fill`}
          customLabel={t`Custom color`}
          onPick={(css, mode) => onStyle({ "background-color": css }, mode)}
        />
      </Row>
      {isSlide ? null : (
        <>
          <Row label={t`Radius`}>
            <SliderField
              label={t`Corner radius`}
              unit="px"
              min={0}
              max={200}
              value={px(c["border-radius"]) ?? 0}
              onChange={(n, mode) => onStyle({ "border-radius": `${num(n)}px` }, mode)}
            />
          </Row>
          <Row label={t`Padding`}>
            <NumberField
              label={t`Padding`}
              unit="px"
              min={0}
              max={400}
              value={px(c["padding-top"]) ?? 0}
              onChange={(n, mode) => onStyle({ padding: `${num(n)}px` }, mode)}
            />
          </Row>
          <Row label={t`Opacity`}>
            <SliderField
              label={t`Opacity`}
              unit="%"
              min={0}
              max={100}
              value={Math.round((Number(c.opacity) || 0) * 100)}
              onChange={(n, mode) => onStyle({ opacity: String(num(n / 100)) }, mode)}
            />
          </Row>
        </>
      )}
    </Section>
  );
}

function PositionSection({ primary, onStyle }: StylePanelProps & { primary: DeckEditTarget }) {
  const { t } = useLingui();
  const at = readTranslate(primary.inline.transform);
  const move = (next: { x: number; y: number }, mode: ChangeMode) =>
    onStyle({ transform: withTranslate(primary.inline.transform, next) }, mode);
  return (
    <Section title={t`Position and size`}>
      <Row label={t`Offset`}>
        <NumberField
          label={t`Offset X`}
          unit="x"
          value={at.x}
          min={-1920}
          max={1920}
          onChange={(n, mode) => move({ ...at, x: n }, mode)}
        />
        <NumberField
          label={t`Offset Y`}
          unit="y"
          value={at.y}
          min={-1080}
          max={1080}
          onChange={(n, mode) => move({ ...at, y: n }, mode)}
        />
      </Row>
      <Row label={t`Size`}>
        <NumberField
          label={t`Width`}
          unit="w"
          value={primary.rect.w}
          min={1}
          max={1920}
          onChange={(n, mode) => onStyle({ width: `${num(n)}px` }, mode)}
        />
        <NumberField
          label={t`Height`}
          unit="h"
          value={primary.rect.h}
          min={1}
          max={1080}
          onChange={(n, mode) => onStyle({ height: `${num(n)}px` }, mode)}
        />
      </Row>
    </Section>
  );
}

function AttributeSection({
  primary,
  onAttributes,
  onCommit,
}: StylePanelProps & { primary: DeckEditTarget }) {
  const { t } = useLingui();
  const link = primary.kind === "link";
  const [value, setValue] = useState((link ? primary.href : primary.alt) ?? "");
  return (
    <Section title={link ? t`Link` : t`Image`}>
      <Row label={link ? t`Address` : t`Description`}>
        <Input
          aria-label={link ? t`Link address` : t`Image description`}
          value={value}
          onChange={(event) => setValue(event.target.value)}
          onBlur={() => {
            const current = (link ? primary.href : primary.alt) ?? "";
            if (value === current) return;
            onAttributes(link ? { href: value || null } : { alt: value || null });
            onCommit();
          }}
          className="h-7 text-[12px]"
        />
      </Row>
    </Section>
  );
}
