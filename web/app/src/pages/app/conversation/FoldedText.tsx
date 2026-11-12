import { useLingui } from "@lingui/react/macro";
import { useState } from "react";

/** How much of a long message the transcript shows before "Show all". */
export const FOLD_AT = 1_500;

/** The shown head of a long text: cut near `limit` at a space or line break when one is close,
 * never inside a surrogate pair. Texts at or under the limit come back whole. */
export function foldedHead(text: string, limit = FOLD_AT): string {
  if (text.length <= limit) return text;
  let cut = limit;
  const lastBreak = Math.max(text.lastIndexOf(" ", limit), text.lastIndexOf("\n", limit));
  if (lastBreak > limit * 0.8) cut = lastBreak;
  const code = text.charCodeAt(cut - 1);
  if (code >= 0xd800 && code <= 0xdbff) cut -= 1;
  return text.slice(0, cut).trimEnd();
}

/**
 * A person's message in the transcript: a very long one (a pasted document, a log) shows its
 * first ~1,500 characters and a "Show all" toggle, so the page never lays out hundreds of
 * thousands of characters until asked. Copy reads the message itself, never this view.
 */
export function FoldedText({ text }: { text: string }) {
  const { t, i18n } = useLingui();
  const [open, setOpen] = useState(false);
  if (text.length <= FOLD_AT) return <>{text}</>;
  const count = text.length.toLocaleString(i18n.locale || undefined);
  return (
    <>
      {open ? text : `${foldedHead(text)}…`}
      <span className="mt-2 block">
        <button
          type="button"
          data-testid="message-fold-toggle"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
          className="rounded-md text-[13px] font-medium underline underline-offset-2 opacity-80 hover:opacity-100 focus-visible:outline-2 focus-visible:outline-ring"
        >
          {open ? t`Show less` : t`Show all (${count} characters)`}
        </button>
      </span>
    </>
  );
}
