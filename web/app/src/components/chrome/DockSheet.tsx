import { useLingui } from "@lingui/react/macro";
import { cn, Dialog, DialogContent, DialogDescription, DialogTitle } from "@superchat/ui-web";
import { X } from "lucide-react";
import type { ReactNode } from "react";

/**
 * A dock place as a large centered glass sheet over the dimmed home, with a close button.
 * The body brings its own heading (`DockSheetHeader`) and content.
 */
export function DockSheet({
  open,
  title,
  onClose,
  onEscape,
  bare = false,
  className,
  children,
}: {
  open: boolean;
  /** The sheet's accessible name; the body shows its own heading. */
  title: string;
  onClose: () => void;
  /** Escape goes here first; true when it was handled (a step back inside the sheet). */
  onEscape?: () => boolean;
  /** The body fills the sheet edge to edge and brings its own close (a file in the editor). */
  bare?: boolean;
  className?: string;
  children: ReactNode;
}) {
  const { t } = useLingui();
  return (
    <Dialog
      open={open}
      onOpenChange={(next, details) => {
        if (next) return;
        if (details?.reason === "escape-key" && onEscape?.()) {
          details.cancel();
          return;
        }
        onClose();
      }}
    >
      <DialogContent
        showCloseButton={false}
        data-testid="dock-sheet"
        className={cn(
          "sc-sheet fixed top-1/2 left-1/2 flex h-[min(720px,calc(100%-3rem))] w-[min(1180px,calc(100%-1.5rem))] max-w-none -translate-x-1/2 -translate-y-1/2 flex-col gap-5 overflow-auto rounded-3xl bg-popover/[0.97] p-5 backdrop-blur-[48px] backdrop-saturate-150 text-foreground shadow-float ring-1 ring-glass-line sm:max-w-none md:left-[calc(50%+44px)] md:w-[min(1180px,calc(100%-9rem))] md:p-7",
          bare && "gap-0 overflow-hidden p-0 md:p-0",
          className,
        )}
      >
        <DialogTitle className="sr-only">{title}</DialogTitle>
        <DialogDescription className="sr-only">{title}</DialogDescription>
        {/* Hidden while a dialog opened from the sheet is on top: that dialog's own close is
            the one to use, so only one close shows. */}
        {bare ? null : (
          <button
            type="button"
            aria-label={t`Close`}
            onClick={onClose}
            className="absolute top-3.5 right-3.5 z-10 [[data-nested-dialog-open]>&]:hidden grid size-11 place-items-center rounded-xl text-foreground transition-colors hover:bg-foreground/10 focus-visible:outline-2 focus-visible:outline-ring"
          >
            <X size={20} strokeWidth={2.2} aria-hidden="true" />
          </button>
        )}
        {children}
      </DialogContent>
    </Dialog>
  );
}

/** A sheet's heading row: the title and a quiet line under it, with controls on the right. */
export function DockSheetHeader({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-4 pe-12">
      <div className="flex min-w-0 flex-col gap-1">
        <h2 className="text-[26px] leading-tight font-[650] tracking-[-0.02em]">{title}</h2>
        {subtitle ? <p className="text-[14px] text-ink-2">{subtitle}</p> : null}
      </div>
      {children}
    </div>
  );
}
