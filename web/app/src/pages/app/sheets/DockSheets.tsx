import { Trans, useLingui } from "@lingui/react/macro";
import { Plus, Upload } from "lucide-react";
import { useRef, useState } from "react";
import { useFileStudio } from "../../../components/cards/context";
import { DockSheet, DockSheetHeader } from "../../../components/chrome/DockSheet";
import { QUIET_BUTTON } from "../../../components/ui";
import { WaitingSheet } from "../../../features/approvals";
import { LibraryScreen } from "../../../features/artifacts/LibraryScreen";
import { LibraryArtifactView } from "../../../features/artifacts/library/LibraryArtifactView";
import { AutomationsSheet } from "../../../features/automations";
import { FilesTab } from "../../../features/files/FilesTab";
import { rpc } from "../../../lib/rpc";
import type { DockKey } from "../chrome/Dock";
import { readFileAsBase64 } from "../conversation/shared";

export function LibrarySheet({
  botId,
  open,
  onClose,
  onNewApp,
  onSendIdea,
}: {
  botId: string;
  open: boolean;
  onClose: () => void;
  onNewApp: () => void;
  onSendIdea: (text: string) => void;
}) {
  const { t } = useLingui();
  // A file opens in the File Studio (ADR 0030), over the app; the Library closes behind it.
  const studio = useFileStudio();
  const openFile = (id: string) => {
    onClose();
    studio?.open(id);
  };
  return (
    <DockSheet open={open} title={t`Library`} onClose={onClose}>
      <DockSheetHeader title={t`Library`} subtitle={t`Apps, decks and documents I made for you.`}>
        <button type="button" className={QUIET_BUTTON} onClick={onNewApp}>
          <Plus size={15} strokeWidth={2} aria-hidden="true" className="me-1 -ms-0.5" />
          <Trans>New app</Trans>
        </button>
      </DockSheetHeader>
      {open ? (
        <div className="sc-scroll -mx-1 flex min-h-0 flex-1 flex-col overflow-y-auto px-1">
          <LibraryScreen botId={botId} onSendIdea={onSendIdea} onOpenArtifact={openFile} />
        </div>
      ) : null}
    </DockSheet>
  );
}

function FilesSheet({
  botId,
  open,
  onClose,
  refreshKey,
  reveal,
}: {
  botId: string;
  open: boolean;
  onClose: () => void;
  refreshKey: number;
  reveal: { path: string; nonce: number } | null;
}) {
  const { t } = useLingui();
  const [uploads, setUploads] = useState(0);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  async function upload(files: FileList | null) {
    if (!files?.length) return;
    setBusy(true);
    setUploadError(null);
    try {
      for (const file of Array.from(files)) {
        await rpc.files.uploadAttachment({
          botId,
          name: file.name,
          mimeType: file.type || "application/octet-stream",
          contentBase64: await readFileAsBase64(file),
        });
      }
      setUploads((count) => count + 1);
    } catch (cause) {
      setUploadError(cause instanceof Error ? cause.message : t`Could not upload this.`);
    } finally {
      setBusy(false);
      if (input.current) input.current.value = "";
    }
  }

  return (
    <DockSheet open={open} title={t`Files`} onClose={onClose}>
      <DockSheetHeader title={t`Files`} subtitle={t`Your workspace.`} />
      {open ? (
        <div className="flex min-h-0 flex-1 flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <input
              ref={input}
              type="file"
              multiple
              hidden
              data-testid="files-upload-input"
              onChange={(event) => void upload(event.target.files)}
            />
            <button
              type="button"
              className={QUIET_BUTTON}
              disabled={busy}
              onClick={() => input.current?.click()}
            >
              <Upload size={15} aria-hidden="true" className="me-1.5" />
              {busy ? t`Uploading…` : t`Upload`}
            </button>
            {uploadError ? (
              <span className="text-[13px] text-destructive">{uploadError}</span>
            ) : null}
          </div>
          <div className="sc-scroll min-h-0 flex-1 overflow-y-auto">
            <FilesTab botId={botId} refreshKey={refreshKey + uploads} reveal={reveal} />
          </div>
        </div>
      ) : null}
    </DockSheet>
  );
}

/**
 * The four places the dock opens, each a large glass sheet over the dimmed home or the
 * Conversation. The Shell owns which one is open and what "start a chat" means.
 */
export function DockSheets({
  botId,
  sheet,
  onClose,
  filesRefreshKey,
  filesReveal,
  onStartChat,
}: {
  botId: string;
  sheet: DockKey | null;
  onClose: () => void;
  filesRefreshKey: number;
  filesReveal: { path: string; nonce: number } | null;
  /** Opens the Conversation; with `send` the text goes at once, with `seed` it waits in the composer. */
  onStartChat: (input: { send?: string; seed?: string }) => void;
}) {
  const sendAndClose = (text: string) => {
    onClose();
    onStartChat({ send: text });
  };
  return (
    <>
      <WaitingSheet
        botId={botId}
        open={sheet === "inbox"}
        onOpenChange={(open) => !open && onClose()}
      />
      <LibrarySheet
        botId={botId}
        open={sheet === "apps"}
        onClose={onClose}
        onNewApp={() => {
          onClose();
          onStartChat({ seed: "Build me an app that " });
        }}
        onSendIdea={sendAndClose}
      />
      <AutomationsSheet
        botId={botId}
        open={sheet === "automations"}
        onClose={onClose}
        renderPage={({ artifactId, back, close }) => (
          <LibraryArtifactView artifactId={artifactId} onBack={back} onClose={close} />
        )}
      />
      <FilesSheet
        botId={botId}
        open={sheet === "files"}
        onClose={onClose}
        refreshKey={filesRefreshKey}
        reveal={filesReveal}
      />
    </>
  );
}
