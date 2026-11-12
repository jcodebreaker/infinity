import { Trans, useLingui } from "@lingui/react/macro";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@superchat/ui-web";
import { useState } from "react";
import { PRIMARY_BUTTON, QUIET_BUTTON } from "../../components/ui";
import { FilesTab } from "./FilesTab";

/**
 * Picks a workspace folder to attach to one message. Nothing is copied: the message carries a
 * pointer, and the Muse sends a Helper to read it.
 */
export function FolderPicker({
  botId,
  open,
  onClose,
  onPick,
}: {
  botId: string;
  open: boolean;
  onClose: () => void;
  /** Workspace-relative; `""` is the whole workspace. */
  onPick: (path: string) => void;
}) {
  const { t } = useLingui();
  const [folder, setFolder] = useState<string | null>(null);
  const close = () => {
    setFolder(null);
    onClose();
  };
  const name = folder ? (folder.split("/").pop() ?? folder) : null;
  return (
    <Dialog open={open} onOpenChange={(next) => (next ? undefined : close())}>
      <DialogContent className="sc-sheet flex max-h-[min(640px,calc(100%-3rem))] w-[min(620px,calc(100%-1.5rem))] flex-col gap-4 rounded-3xl bg-popover/95 p-6 text-foreground ring-1 ring-glass-line sm:max-w-none">
        <div className="flex flex-col gap-1">
          <DialogTitle className="text-[22px] font-[650] tracking-[-0.02em]">
            {t`Attach a folder`}
          </DialogTitle>
          <DialogDescription className="text-[14px] text-ink-2">
            {t`superchat reads it where it is; nothing is copied.`}
          </DialogDescription>
        </div>
        <div className="sc-scroll min-h-[240px] flex-1 overflow-y-auto rounded-2xl bg-black/20 p-1.5">
          {open ? (
            <FilesTab
              botId={botId}
              refreshKey={0}
              selectedFolder={folder}
              onSelectFolder={(path) => setFolder((current) => (current === path ? null : path))}
            />
          ) : null}
        </div>
        <div className="flex justify-end gap-2">
          <button type="button" className={`${QUIET_BUTTON} h-11 px-4`} onClick={close}>
            <Trans>Cancel</Trans>
          </button>
          <button
            type="button"
            className={`${PRIMARY_BUTTON} h-11 px-5`}
            disabled={folder === null}
            onClick={() => {
              if (folder === null) return;
              onPick(folder);
              close();
            }}
          >
            {name ? t`Attach ${name}` : t`Pick a folder`}
          </button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
