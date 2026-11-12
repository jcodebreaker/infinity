import { useLingui } from "@lingui/react/macro";
import type { Artifact, MessageArtifact } from "@superchat/contracts";
import { cn, Popover, PopoverContent, PopoverTrigger } from "@superchat/ui-web";
import { Check } from "lucide-react";
import { useEffect, useState } from "react";
import { ComposerAttachmentChip } from "../../../components/ComposerAttachmentChip";
import { formatRelativeTime } from "../../../lib/relative-time";
import { rpc } from "../../../lib/rpc";

/** How many of the conversation's artifacts the chip offers. */
const RECENT_LIMIT = 8;

/** The chip text: the file name on one line, capped like an element ask's. */
export const artifactChipLabel = (name: string): string =>
  name.replace(/\s+/g, " ").trim().slice(0, 60);

/** The message's own link wins over a pick still in flight only once it changes. */
const linkKey = (artifact: MessageArtifact | null | undefined) =>
  artifact ? `${artifact.artifactId}:${artifact.version}:${artifact.how}` : "none";

type Recent = Pick<Artifact, "id" | "name" | "createdAt">;

/**
 * The chip on a person's message that says which artifact it is about (docs/conversation.md).
 * A wrong guess is one tap: the chip opens the conversation's recent artifacts plus "None", and
 * a pick relinks the message at once, reverting with a quiet note when the engine refuses.
 */
export function MessageArtifactChip({
  botId,
  itemId,
  artifact,
}: {
  botId: string;
  /** The person's message (`optimistic` until the engine has recorded it). */
  itemId: string;
  artifact: MessageArtifact;
}) {
  const { t } = useLingui();
  // A pick shown before the engine's transcript has it (`null`: none), for the link it replaced:
  // once the message's own link changes, that wins.
  const serverKey = linkKey(artifact);
  const [local, setLocal] = useState<{ over: string; value: MessageArtifact | null } | null>(null);
  const picked = local?.over === serverKey ? local.value : undefined;
  const setPicked = (value: MessageArtifact | null | undefined) =>
    setLocal(value === undefined ? null : { over: serverKey, value });
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(false);
  const [recent, setRecent] = useState<Recent[] | null>(null);
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    void rpc.artifacts
      .listSpace({ botId, limit: RECENT_LIMIT })
      .then((page) => {
        if (!cancelled) setRecent(page.items);
      })
      .catch(() => {
        if (!cancelled) setRecent([]);
      });
    return () => {
      cancelled = true;
    };
  }, [botId, open]);

  const shown = picked === undefined ? artifact : picked;
  if (!shown) return null;
  const label = artifactChipLabel(shown.name);
  // Sent but not yet recorded: there is no message to relink yet.
  if (itemId === "optimistic") {
    return <ComposerAttachmentChip kind="element" label={label} testId="message-artifact-chip" />;
  }

  const isCurrent = (item: Recent) => item.id === shown.artifactId || item.name === shown.name;
  const listed = recent ?? [];
  const rows: Recent[] = listed.some(isCurrent)
    ? listed
    : [{ id: shown.artifactId, name: shown.name, createdAt: "" }, ...listed];

  const pick = (next: Recent | null) => {
    setOpen(false);
    setFailed(false);
    const previous = picked;
    setPicked(next ? { artifactId: next.id, name: next.name, version: null, how: "chosen" } : null);
    rpc.chats
      .setMessageArtifact({ botId, itemId, artifactId: next?.id ?? null })
      .then((updated) => setPicked(updated.artifact ?? null))
      .catch(() => {
        setPicked(previous);
        setFailed(true);
      });
  };

  const row = (key: string, text: string, current: boolean, onClick: () => void, when = "") => (
    <button
      key={key}
      type="button"
      data-testid="message-artifact-option"
      aria-pressed={current}
      onClick={onClick}
      className="flex min-h-8 w-full items-center gap-2 rounded-md px-2 text-start text-[13px] text-foreground outline-none hover:bg-accent focus-visible:bg-accent"
    >
      <Check size={14} aria-hidden="true" className={cn("shrink-0", !current && "invisible")} />
      <span className="min-w-0 flex-1 truncate" dir="auto">
        {text}
      </span>
      {when ? <span className="shrink-0 text-[11.5px] text-muted-foreground">{when}</span> : null}
    </button>
  );

  return (
    <div className="flex max-w-full items-center gap-2">
      {failed ? (
        <span data-testid="message-artifact-failed" className="text-[12px] text-muted-foreground">
          {t`Couldn't change`}
        </span>
      ) : null}
      <Popover
        open={open}
        onOpenChange={(next) => {
          setOpen(next);
          if (next) setFailed(false);
        }}
      >
        <PopoverTrigger
          nativeButton={false}
          render={
            <ComposerAttachmentChip
              kind="element"
              label={label}
              testId="message-artifact-chip"
              className="cursor-pointer outline-none hover:bg-accent focus-visible:ring-3 focus-visible:ring-ring/50 data-[popup-open]:bg-accent"
            />
          }
        />
        <PopoverContent align="end" className="w-64 gap-0 p-1">
          {rows.map((item) =>
            row(
              item.id,
              item.name,
              isCurrent(item),
              () => pick(item),
              item.createdAt ? formatRelativeTime(item.createdAt) : "",
            ),
          )}
          {row("none", t`None`, false, () => pick(null))}
        </PopoverContent>
      </Popover>
    </div>
  );
}
