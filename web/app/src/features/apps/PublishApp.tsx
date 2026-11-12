import { plural } from "@lingui/core/macro";
import { useLingui } from "@lingui/react/macro";
import type { ArtifactPublish, ArtifactPublishAudience } from "@superchat/contracts";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  Button,
  cn,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Label,
  RadioGroup,
  RadioGroupItem,
} from "@superchat/ui-web";
import { Check, Copy } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import type { RegisterPublishSave } from "../artifacts";

const AUDIENCES: ArtifactPublishAudience[] = ["owner", "org", "link"];

/** Where a published app opens: Superchat's own origin plus its `/apps/<slug>` path. */
export function appAddress(urlPath: string): string {
  return `${window.location.origin}${urlPath}`;
}

export function useAudienceLabels() {
  const { t } = useLingui();
  return {
    owner: { label: t`Only me`, hint: t`Just you, signed in` },
    org: { label: t`My organization`, hint: t`Anyone signed in to your organization` },
    link: { label: t`Anyone with the link`, hint: t`No sign-in needed` },
  } satisfies Record<ArtifactPublishAudience, { label: string; hint: string }>;
}

/** "N opens" / "N opens · M viewers", the same wording on the bar and the Library card. */
export function viewsText(stats: ArtifactPublish["stats"]): string {
  const opens = plural(stats.opensTotal, { one: "# open", other: "# opens" });
  if (stats.uniqueViewers <= 1) return opens;
  const viewers = plural(stats.uniqueViewers, { one: "# viewer", other: "# viewers" });
  return `${opens} · ${viewers}`;
}

/** The small "Published" marker beside a title. */
export function PublishedPill({ className }: { className?: string }) {
  const { t } = useLingui();
  return (
    <span
      data-testid="published-pill"
      className={cn(
        "inline-flex shrink-0 items-center rounded-full bg-success/15 px-2 py-0.5 text-[11px] font-medium text-success",
        className,
      )}
    >
      {t`Published`}
    </span>
  );
}

export function PublishedBar({
  publish,
  onSettings,
  onUnpublish,
}: {
  publish: ArtifactPublish;
  onSettings: () => void;
  onUnpublish: () => Promise<void>;
}) {
  const { t } = useLingui();
  const labels = useAudienceLabels();
  const [copied, setCopied] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const address = appAddress(publish.urlPath);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1600);
    return () => window.clearTimeout(timer);
  }, [copied]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(address);
      setCopied(true);
    } catch {
      // Clipboard blocked; the address stays selectable in the bar.
    }
  }

  return (
    <div
      data-testid="published-bar"
      className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-border bg-success/10 px-4 py-2.5"
    >
      <span
        className="min-w-0 flex-[1_1_220px] select-all truncate font-mono text-[12.5px] text-foreground"
        dir="ltr"
      >
        {address}
      </span>
      <span className="text-[12px] text-muted-foreground">
        {labels[publish.audience].label} · {viewsText(publish.stats)}
      </span>
      <Button variant="outline" size="sm" onClick={() => void copy()}>
        {copied ? <Check size={13} /> : <Copy size={13} />}
        {copied ? t`Copied` : t`Copy link`}
      </Button>
      <Button variant="ghost" size="sm" onClick={onSettings}>
        {t`Publish settings`}
      </Button>
      <Button
        variant="ghost"
        size="sm"
        className="text-destructive hover:text-destructive"
        onClick={() => setConfirming(true)}
      >
        {t`Unpublish`}
      </Button>
      <AlertDialog open={confirming} onOpenChange={(open) => !busy && setConfirming(open)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t`Unpublish this app?`}</AlertDialogTitle>
            <AlertDialogDescription>
              {t`Its address stops working right away. You can publish it again later.`}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>{t`Cancel`}</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={busy}
              onClick={(event) => {
                event.preventDefault();
                setBusy(true);
                void onUnpublish()
                  .then(() => setConfirming(false))
                  .finally(() => setBusy(false));
              }}
            >
              {t`Unpublish`}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

/**
 * "Publish…" (and, once published, "Publish settings"): the person's click on Publish is the
 * approval, so there is no further ask. The backend owns who may publish and what.
 */
export function PublishDialog({
  name,
  current,
  onClose,
  onConfirm,
  options,
}: {
  name: string;
  /** The existing publication when this edits its settings. */
  current: ArtifactPublish | null;
  onClose: () => void;
  onConfirm: (audience: ArtifactPublishAudience) => Promise<void>;
  /** Extra fields from other capabilities; they save when the person confirms. */
  options?: (register: RegisterPublishSave) => ReactNode;
}) {
  const { t } = useLingui();
  const labels = useAudienceLabels();
  const [audience, setAudience] = useState<ArtifactPublishAudience>(current?.audience ?? "owner");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const saves = useRef(new Set<() => Promise<void>>());
  const register: RegisterPublishSave = (save) => {
    saves.current.add(save);
    return () => void saves.current.delete(save);
  };

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      await onConfirm(audience);
      await Promise.all([...saves.current].map((save) => save()));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t`Could not publish this.`);
      setBusy(false);
    }
  }

  return (
    <Dialog open onOpenChange={(open) => !open && !busy && onClose()}>
      <DialogContent className="sm:max-w-md" data-testid="publish-dialog">
        <DialogHeader>
          <DialogTitle>{current ? t`Publish settings` : t`Publish ${name}?`}</DialogTitle>
          <DialogDescription>
            {t`It gets its own web address. It runs on its own, separate from superchat, and can't see your conversations or files.`}
          </DialogDescription>
        </DialogHeader>
        <fieldset className="grid gap-2 border-0 p-0">
          <legend className="mb-1.5 text-[12px] text-muted-foreground">{t`Who can open it`}</legend>
          <RadioGroup
            value={audience}
            onValueChange={(value) => setAudience(value as ArtifactPublishAudience)}
            aria-label={t`Who can open it`}
          >
            {AUDIENCES.map((option) => (
              <Label
                key={option}
                className={cn(
                  "flex cursor-pointer items-start gap-2.5 rounded-lg border px-3 py-2.5 transition-colors",
                  audience === option ? "border-ring bg-accent" : "border-border",
                )}
              >
                <RadioGroupItem value={option} className="mt-0.5" />
                <span className="flex flex-col gap-0.5">
                  <span className="text-[13px] font-medium">{labels[option].label}</span>
                  <span className="text-[12px] text-muted-foreground">{labels[option].hint}</span>
                </span>
              </Label>
            ))}
          </RadioGroup>
        </fieldset>
        {options?.(register)}
        <div className="truncate font-mono text-[12px] text-muted-foreground" dir="ltr">
          {current ? appAddress(current.urlPath) : `${window.location.host}/apps/…`}
        </div>
        {error ? <p className="text-[13px] text-destructive">{error}</p> : null}
        <DialogFooter>
          <Button variant="ghost" disabled={busy} onClick={onClose}>
            {t`Not now`}
          </Button>
          <Button disabled={busy} onClick={() => void submit()}>
            {current ? t`Save` : t`Publish`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
