import { useSyncExternalStore } from "react";

// What a surface (a sheet range, a deck element) attaches to the person's next message: one slot
// per kind, shown as a chip above the composer and sent as a fenced block ahead of their text. A
// sent message is read back into chips by the parser each kind registers.

/** `block` is the engine-facing untrusted-data block; `label` the chip text. */
export type ComposerAttachment = { kind: string; label: string; block: string };
export type AttachmentChip = { kind: string; label: string };
type Split = { label: string; rest: string };
type Parser = (text: string) => Split | null;

let attachments: readonly ComposerAttachment[] = [];
const parsers = new Map<string, Parser>();
const listeners = new Set<() => void>();
const emit = () => {
  for (const listener of listeners) listener();
};
const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => void listeners.delete(listener);
};

/** Sets (or, with null, clears) the attachment of one kind. */
export function setComposerAttachment(kind: string, next: Omit<ComposerAttachment, "kind"> | null) {
  const others = attachments.filter((a) => a.kind !== kind);
  attachments = next ? [...others, { kind, ...next }] : others;
  emit();
}

export const clearComposerAttachments = (): void => {
  attachments = [];
  emit();
};

export const useComposerAttachments = (): readonly ComposerAttachment[] =>
  useSyncExternalStore(subscribe, () => attachments);

/** The message as sent: every attachment block first, then what the person wrote. */
export function withComposerAttachments(
  text: string,
  current: readonly ComposerAttachment[],
): string {
  const blocks = current.map((a) => a.block);
  return [...blocks, ...(text ? [text] : [])].join("\n\n");
}

/** A kind registers how to read its block back from a sent message (module side effect). */
export function registerAttachmentParser(kind: string, parse: Parser): void {
  parsers.set(kind, parse);
}

/**
 * Splits a sent message into the chips its leading attachment blocks stand for and what the
 * person wrote. Only blocks at the very start count, each matched from its fixed wording.
 */
export function splitComposerAttachments(text: string): { chips: AttachmentChip[]; rest: string } {
  const chips: AttachmentChip[] = [];
  let rest = text;
  for (let guard = 0; guard < 8; guard += 1) {
    let hit: { kind: string; split: Split } | null = null;
    for (const [kind, parse] of parsers) {
      const split = parse(rest);
      if (split) {
        hit = { kind, split };
        break;
      }
    }
    if (!hit) break;
    chips.push({ kind: hit.kind, label: hit.split.label });
    rest = hit.split.rest;
  }
  return { chips, rest };
}
