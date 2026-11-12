import { useLingui } from "@lingui/react/macro";
import type { ReplyCardDataOf, ReplyCardKind } from "@superchat/contracts";
import {
  Badge,
  Button,
  Card,
  CardAction,
  CardContent,
  CardHeader,
  CardTitle,
  cn,
  Skeleton,
} from "@superchat/ui-web";
import {
  ArrowDownRight,
  ArrowUpRight,
  Check,
  Download,
  FileText,
  Link as LinkIcon,
  Mail,
  Minus,
  Phone,
} from "lucide-react";
import { type ReactNode, useRef, useState } from "react";
import { useAskPreviews, useReplyCardSend } from "./context";
import { hostOf, mailtoHref, safeHttpUrl, telHref } from "./links";

/** The transcript bubble is as wide as its content, so cards state their own width. */
const CARD_WIDTH = "w-[min(30rem,calc(100vw-3rem))]";
const WIDE_CARD_WIDTH = "w-[min(42rem,calc(100vw-3rem))]";
/** A question answered with previews (deck looks) fills the message column: tiles side by side. */
const PREVIEW_CARD_WIDTH = "w-[min(46rem,calc(100vw-3rem))] max-w-full";

/** Every card kind shares one frame: compact, monochrome, readable at phone width. */
export function Frame({
  title,
  action,
  flush,
  children,
  className,
}: {
  title?: string;
  action?: ReactNode;
  /** Content runs edge to edge (tables). */
  flush?: boolean;
  children: ReactNode;
  className?: string;
}) {
  return (
    <Card
      size="sm"
      data-testid="reply-card"
      className={cn(CARD_WIDTH, "gap-3", flush && "gap-0 py-0", className)}
    >
      {title || action ? (
        <CardHeader className={cn(flush && "pt-3 pb-2")}>
          {title ? (
            <CardTitle className="text-[14px] font-medium" dir="auto">
              {title}
            </CardTitle>
          ) : null}
          {action ? (
            <CardAction className="text-[12.5px] text-muted-foreground tabular-nums">
              {action}
            </CardAction>
          ) : null}
        </CardHeader>
      ) : null}
      <CardContent className={cn("min-w-0", flush && "px-0")}>{children}</CardContent>
    </Card>
  );
}

type Props<K extends ReplyCardKind> = { title?: string; data: ReplyCardDataOf<K> };

function Sources({ title, data }: Props<"sources">) {
  const { t } = useLingui();
  return (
    <Frame title={title ?? t`Sources`}>
      <ol className="-my-1 flex flex-col divide-y divide-border">
        {data.items.map((item, index) => {
          const href = safeHttpUrl(item.url);
          return (
            <li key={`${item.url}-${index}`} className="flex gap-3 py-2.5">
              <span className="w-4 shrink-0 pt-px text-end text-[12px] text-muted-foreground tabular-nums">
                {index + 1}
              </span>
              <div className="min-w-0 flex-1">
                {href ? (
                  <a
                    href={href}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="block text-[14px] leading-snug font-medium text-foreground hover:underline"
                    dir="auto"
                  >
                    {item.title}
                  </a>
                ) : (
                  <span className="block text-[14px] leading-snug font-medium" dir="auto">
                    {item.title}
                  </span>
                )}
                <span className="text-[12px] text-muted-foreground">
                  {href ? hostOf(href) : null}
                </span>
                {item.snippet ? (
                  <p
                    className="mt-0.5 line-clamp-2 text-[13px] leading-[1.45] text-muted-foreground"
                    dir="auto"
                  >
                    {item.snippet}
                  </p>
                ) : null}
              </div>
            </li>
          );
        })}
      </ol>
    </Frame>
  );
}

function Compare({ title, data }: Props<"compare">) {
  return (
    <Frame title={title} flush className={WIDE_CARD_WIDTH}>
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-[13px]">
          <thead>
            <tr className="border-y border-border bg-muted/50">
              <td className="sticky left-0 bg-muted/50" />
              {data.columns.map((column, index) => (
                <th
                  key={`${column}-${index}`}
                  scope="col"
                  className="min-w-32 px-3 py-2 text-start font-medium text-foreground"
                  dir="auto"
                >
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.rows.map((row, rowIndex) => (
              <tr key={`${row.label}-${rowIndex}`} className="border-b border-border last:border-0">
                <th
                  scope="row"
                  className="sticky left-0 z-10 min-w-24 bg-card px-3 py-2.5 text-start align-top font-medium text-muted-foreground"
                  dir="auto"
                >
                  {row.label}
                </th>
                {data.columns.map((column, index) => (
                  <td
                    key={`${column}-${index}`}
                    className="px-3 py-2.5 align-top leading-[1.45] text-foreground/90"
                    dir="auto"
                  >
                    {row.cells[index] ?? <Minus size={13} className="text-muted-foreground/50" />}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Frame>
  );
}

function Ask({
  title,
  data,
  answer,
  cardId,
}: Props<"ask"> & {
  /** What the person replied (their next message); locks the card. */
  answer?: string;
  /** Names this card in the structured answer sent beside the label. */
  cardId?: string;
}) {
  const send = useReplyCardSend();
  const [picked, setPicked] = useState<string | null>(null);
  // A second click can land before the locked state paints: the ref makes one send certain.
  const pickedRef = useRef(false);
  const chosen = picked ?? answer?.trim() ?? null;
  const locked = chosen !== null || !send;
  const pick = (option: { id: string; label: string } | undefined) => {
    if (!option || locked || pickedRef.current) return;
    pickedRef.current = true;
    const { label } = option;
    setPicked(label);
    if (cardId) send?.(label, { cardId, optionId: option.id, label });
    else send?.(label);
  };
  // Options that preview something (deck themes) are drawn by that capability's own tiles.
  const previewKind = data.options.find((o) => o.preview)?.preview?.kind;
  const Tiles = useAskPreviews()[previewKind ?? ""];
  return (
    <Frame title={title} className={Tiles ? PREVIEW_CARD_WIDTH : undefined}>
      <p className="text-[14.5px] leading-snug font-medium" dir="auto">
        {data.question}
      </p>
      {Tiles ? (
        <Tiles
          options={data.options.map((option) => ({
            id: option.id,
            label: option.label,
            previewId: option.preview?.id ?? "",
          }))}
          chosen={chosen}
          locked={locked}
          // The tiles answer with a label (AskPreviewProps): its option.
          onPick={(label) => pick(data.options.find((o) => o.label === label))}
        />
      ) : (
        <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:flex-wrap">
          {data.options.map((option) => {
            const isChosen = chosen === option.label;
            return (
              <Button
                key={option.id}
                type="button"
                variant={isChosen ? "default" : "outline"}
                disabled={locked && !isChosen}
                aria-pressed={locked ? isChosen : undefined}
                onClick={() => pick(option)}
                className={cn(
                  "h-9 justify-start gap-2 px-3 text-[13.5px] sm:justify-center",
                  isChosen && "disabled:opacity-100",
                )}
              >
                {isChosen ? <Check aria-hidden="true" /> : null}
                <span dir="auto">{option.label}</span>
              </Button>
            );
          })}
        </div>
      )}
    </Frame>
  );
}

/** Zero-width and bidi controls (category Cf) can hide or reorder what a chip says. */
const visibleText = (text: string) => text.replace(/\p{Cf}/gu, "").trim();

/** Quiet next-message chips under the latest answer; a click sends the text as the person's reply. */
function FollowUps({ data }: Props<"follow_ups">) {
  const { t } = useLingui();
  const send = useReplyCardSend();
  const [sent, setSent] = useState(false);
  // A second click can land before the disabled state paints: the ref makes one send certain.
  const sentRef = useRef(false);
  return (
    <fieldset
      data-testid="follow-ups"
      className="m-0 flex min-w-0 max-w-full flex-wrap gap-1.5 border-0 p-0"
    >
      <legend className="sr-only">{t`Suggested follow-ups`}</legend>
      {data.suggestions.map((suggestion) => {
        const text = visibleText(suggestion);
        return (
          <button
            key={text}
            type="button"
            disabled={!send || sent}
            onClick={() => {
              if (sentRef.current || !send) return;
              sentRef.current = true;
              setSent(true);
              send(text);
            }}
            className="max-w-full cursor-pointer rounded-full border border-border/70 px-3 py-1 text-start text-[13px] leading-snug text-muted-foreground transition-colors hover:border-border hover:bg-muted/60 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:outline-none disabled:cursor-default disabled:opacity-60"
            dir="auto"
          >
            {text}
          </button>
        );
      })}
    </fieldset>
  );
}

function formatPrice(value: number, currency?: string): string {
  try {
    if (currency && /^[A-Za-z]{3}$/.test(currency)) {
      return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(value);
    }
  } catch {
    // An unknown currency code falls through to a plain number.
  }
  const plain = new Intl.NumberFormat(undefined, { maximumFractionDigits: 4 }).format(value);
  return currency ? `${plain} ${currency}` : plain;
}

const signed = (value: number, digits: number) =>
  `${value > 0 ? "+" : value < 0 ? "−" : ""}${Math.abs(value).toLocaleString(undefined, {
    maximumFractionDigits: digits,
  })}`;

function Quote({ title, data }: Props<"quote">) {
  const direction = Math.sign(data.change ?? data.changePct ?? 0);
  const Arrow = direction > 0 ? ArrowUpRight : direction < 0 ? ArrowDownRight : Minus;
  const meta = [data.asOf, data.source].filter(Boolean).join(" · ");
  const change = [
    data.change !== undefined ? signed(data.change, 2) : null,
    data.changePct !== undefined ? `${signed(data.changePct, 2)}%` : null,
  ].filter(Boolean);
  return (
    <Frame title={title}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Badge variant="secondary">{data.symbol}</Badge>
            {data.name ? (
              <span className="truncate text-[13px] text-muted-foreground" dir="auto">
                {data.name}
              </span>
            ) : null}
          </div>
          <div className="mt-2 text-[28px] leading-none font-semibold tracking-tight tabular-nums">
            {formatPrice(data.price, data.currency)}
          </div>
        </div>
        {change.length ? (
          <div
            className={cn(
              "flex shrink-0 items-center gap-0.5 pt-0.5 text-[13px] font-medium tabular-nums",
              direction > 0 && "text-success",
              direction < 0 && "text-destructive",
              direction === 0 && "text-muted-foreground",
            )}
          >
            <Arrow size={15} aria-hidden="true" />
            {change.length === 2 ? `${change[0]} (${change[1]})` : change[0]}
          </div>
        ) : null}
      </div>
      {meta ? <p className="mt-2.5 text-[12px] text-muted-foreground">{meta}</p> : null}
    </Frame>
  );
}

function Person({ title, data }: Props<"person">) {
  const href = safeHttpUrl(data.url);
  const initials = data.name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => Array.from(part)[0]?.toUpperCase())
    .join("");
  const subtitle = [data.role, data.org].filter(Boolean).join(" · ");
  const rows: { key: string; icon: ReactNode; text: string; href: string | null }[] = [];
  if (data.email)
    rows.push({ key: "email", icon: <Mail />, text: data.email, href: mailtoHref(data.email) });
  if (data.phone)
    rows.push({ key: "phone", icon: <Phone />, text: data.phone, href: telHref(data.phone) });
  if (href) rows.push({ key: "url", icon: <LinkIcon />, text: hostOf(href), href });
  return (
    <Frame title={title}>
      <div className="flex items-center gap-3">
        <span
          aria-hidden="true"
          className="grid size-10 shrink-0 place-items-center rounded-full bg-muted text-[14px] font-medium text-foreground"
        >
          {initials}
        </span>
        <div className="min-w-0">
          <div className="truncate text-[14.5px] font-medium" dir="auto">
            {data.name}
          </div>
          {subtitle ? (
            <div className="truncate text-[13px] text-muted-foreground" dir="auto">
              {subtitle}
            </div>
          ) : null}
        </div>
      </div>
      {rows.length ? (
        <ul className="mt-3 flex flex-col gap-1.5 text-[13px]">
          {rows.map((row) => (
            <li key={row.key} className="flex min-w-0 items-center gap-2 text-muted-foreground">
              <span className="shrink-0 [&>svg]:size-3.5">{row.icon}</span>
              {row.href ? (
                <a
                  href={row.href}
                  {...(row.key === "url" ? { target: "_blank", rel: "noopener noreferrer" } : {})}
                  className="truncate text-foreground hover:underline"
                >
                  {row.text}
                </a>
              ) : (
                <span className="truncate text-foreground">{row.text}</span>
              )}
            </li>
          ))}
        </ul>
      ) : null}
    </Frame>
  );
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toLocaleString(undefined, { maximumFractionDigits: value < 10 ? 1 : 0 })} ${units[unit]}`;
}

function File({ title, data }: Props<"file">) {
  const { t } = useLingui();
  const href = safeHttpUrl(data.url);
  const meta = [data.kind, data.size !== undefined ? formatSize(data.size) : null]
    .filter(Boolean)
    .join(" · ");
  return (
    <Frame title={title}>
      <div className="flex items-center gap-3">
        <span
          aria-hidden="true"
          className="grid size-10 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground"
        >
          <FileText size={18} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="truncate text-[14px] font-medium" dir="auto">
            {data.name}
          </div>
          {meta ? <div className="truncate text-[12.5px] text-muted-foreground">{meta}</div> : null}
        </div>
        {href ? (
          <Button
            variant="outline"
            size="icon"
            aria-label={t`Open ${data.name}`}
            nativeButton={false}
            render={<a href={href} target="_blank" rel="noopener noreferrer" />}
          >
            <Download />
          </Button>
        ) : null}
      </div>
    </Frame>
  );
}

function Progress({ title, data }: Props<"progress">) {
  const { t } = useLingui();
  const value = Math.round(Math.min(100, Math.max(0, data.value)));
  const status = { running: t`Running`, done: t`Done`, failed: t`Failed` }[data.status];
  return (
    <Frame title={title}>
      <div className="flex items-baseline justify-between gap-3 text-[14px]">
        <span className="min-w-0 truncate" dir="auto">
          {data.label}
        </span>
        <span className="flex shrink-0 items-center gap-1.5 text-[12.5px] text-muted-foreground tabular-nums">
          {data.status === "done" ? <Check size={13} className="text-success" /> : null}
          {data.status === "running" ? `${value}%` : status}
        </span>
      </div>
      <div
        role="progressbar"
        aria-valuenow={value}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuetext={`${status} ${value}%`}
        aria-label={data.label}
        className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-muted"
      >
        <div
          className={cn(
            "h-full rounded-full transition-[width] duration-500 motion-reduce:transition-none",
            data.status === "failed" ? "bg-destructive" : "bg-foreground",
            data.status === "done" && "bg-success",
          )}
          style={{ width: `${value}%` }}
        />
      </div>
    </Frame>
  );
}

/** Shape-matched placeholder while the tool call has no result yet. */
export function CardSkeleton({ card }: { card: string }) {
  const rows = card === "compare" || card === "sources" ? 3 : 1;
  return (
    <Frame>
      <div aria-busy="true" className="flex flex-col gap-2.5">
        <Skeleton className="h-4 w-2/5" />
        {Array.from({ length: rows }, (_, index) => (
          <Skeleton key={index} className={cn("h-3.5", index % 2 ? "w-3/5" : "w-4/5")} />
        ))}
      </div>
    </Frame>
  );
}

export const catalog = {
  sources: Sources,
  compare: Compare,
  ask: Ask,
  follow_ups: FollowUps,
  quote: Quote,
  person: Person,
  file: File,
  progress: Progress,
} as const;
