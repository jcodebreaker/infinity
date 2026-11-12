import { Trans, useLingui } from "@lingui/react/macro";
import type { Artifact } from "@superchat/contracts";
import { Button, Dialog, DialogContent, DialogHeader, DialogTitle } from "@superchat/ui-web";
import { Check, Download, Library, Lock } from "lucide-react";
import { useEffect, useState } from "react";
import { decodeArtifactBase64, downloadArtifactBytes } from "../../lib/artifact-open";
import { rpc } from "../../lib/rpc";
import { useVisiblePoll } from "../../lib/use-visible-poll";
import { ArtifactPreview } from "../artifacts/index";

type Loaded =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "unavailable"; reason: "large" | "binary" }
  | { status: "ready"; artifact: Artifact; bytes: Uint8Array };

type Saved =
  | { status: "idle" }
  | { status: "saving" }
  | { status: "done" }
  | { status: "error"; message: string };

const FILE_POLL_MS = 4000;

const sameBytes = (a: Uint8Array, b: Uint8Array) =>
  a.byteLength === b.byteLength && a.every((value, index) => value === b[index]);

/** Previews one workspace file with the Library's viewers (sandboxed HTML, markdown, PDF, image,
 * text as monospace), with Download and Save to Library. */
export function FilePreviewDialog({
  botId,
  path,
  page,
  onOpenChange,
}: {
  botId: string;
  path: string;
  /** Open a PDF at this page (1-based). */
  page?: number;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useLingui();
  const [state, setState] = useState<Loaded>({ status: "loading" });
  const [saved, setSaved] = useState<Saved>({ status: "idle" });
  const name = path.split("/").pop() ?? path;

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    setSaved({ status: "idle" });
    void rpc.files
      .read({ botId, path })
      .then((file) => {
        if (cancelled) return;
        if (file.contentBase64 === null) {
          setState({ status: "unavailable", reason: file.tooLarge ? "large" : "binary" });
          return;
        }
        setState({
          status: "ready",
          bytes: decodeArtifactBase64(file.contentBase64),
          artifact: {
            id: path,
            botId,
            runId: null,
            name: file.name,
            description: null,
            mimeType: file.mimeType,
            size: file.size,
            version: 1,
            createdAt: new Date().toISOString(),
          },
        });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setState({
            status: "error",
            message: error instanceof Error ? error.message : t`Could not load this.`,
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [botId, path, t]);

  // Superchat may rewrite the open file: re-read it quietly and swap the view only when it changed.
  useVisiblePoll(
    async () => {
      const file = await rpc.files.read({ botId, path });
      if (file.contentBase64 === null) return;
      const bytes = decodeArtifactBase64(file.contentBase64);
      setState((current) =>
        current.status === "ready" && sameBytes(current.bytes, bytes)
          ? current
          : {
              status: "ready",
              bytes,
              artifact:
                current.status === "ready"
                  ? { ...current.artifact, size: file.size, mimeType: file.mimeType }
                  : ({
                      id: path,
                      botId,
                      runId: null,
                      name: file.name,
                      description: null,
                      mimeType: file.mimeType,
                      size: file.size,
                      version: 1,
                      createdAt: new Date().toISOString(),
                    } satisfies Artifact),
            },
      );
    },
    FILE_POLL_MS,
    state.status === "ready",
  );

  const save = () => {
    setSaved({ status: "saving" });
    rpc.files
      .saveToLibrary({ botId, path })
      .then(() => setSaved({ status: "done" }))
      .catch((error: unknown) =>
        setSaved({
          status: "error",
          message: error instanceof Error ? error.message : t`Could not save this.`,
        }),
      );
  };

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[90vh] w-[94vw] max-w-none flex-col gap-0 overflow-hidden p-0 sm:max-w-6xl sm:rounded-3xl">
        <DialogHeader className="shrink-0 flex-row items-center justify-between gap-3 border-b border-border px-5 py-3.5 text-left">
          <DialogTitle className="min-w-0 truncate text-[15px] font-medium">{name}</DialogTitle>
          {state.status === "ready" ? (
            <div className="me-8 flex shrink-0 items-center gap-2">
              {saved.status === "done" ? (
                <span className="inline-flex items-center gap-1.5 text-[12.5px] text-muted-foreground">
                  <Check size={14} strokeWidth={1.9} />
                  <Trans>Saved to Library</Trans>
                </span>
              ) : saved.status === "error" ? (
                <span className="max-w-[240px] truncate text-[12.5px] text-destructive">
                  {saved.message}
                </span>
              ) : null}
              {saved.status !== "done" ? (
                <Button
                  variant="outline"
                  size="sm"
                  disabled={saved.status === "saving"}
                  onClick={save}
                >
                  <Library className="me-1.5" size={14} strokeWidth={1.75} />
                  <Trans>Save to Library</Trans>
                </Button>
              ) : null}
              <Button
                variant="outline"
                size="sm"
                onClick={() =>
                  downloadArtifactBytes(state.artifact.name, state.artifact.mimeType, state.bytes)
                }
              >
                <Download className="me-1.5" size={14} strokeWidth={1.75} />
                <Trans>Download</Trans>
              </Button>
            </div>
          ) : null}
        </DialogHeader>
        <div className="relative min-h-0 flex-1">
          {state.status === "loading" ? (
            <div className="grid h-full place-items-center text-[13.5px] text-muted-foreground">
              <Trans>Loading…</Trans>
            </div>
          ) : state.status === "error" ? (
            <div className="grid h-full place-items-center px-6 text-center text-[13.5px] text-destructive">
              {state.message}
            </div>
          ) : state.status === "unavailable" ? (
            <div className="grid h-full place-items-center px-6 text-center text-[13.5px] text-muted-foreground">
              {state.reason === "large" ? (
                <Trans>Too large to preview.</Trans>
              ) : (
                <Trans>Preview isn't available for this file type.</Trans>
              )}
            </div>
          ) : (
            <>
              <ArtifactPreview artifact={state.artifact} bytes={state.bytes} page={page} />
              {state.artifact.mimeType === "text/html" ? (
                <div className="pointer-events-none absolute bottom-3 end-3 flex items-center gap-1.5 rounded-full bg-black/70 px-2.5 py-1.5 text-[11px] text-white">
                  <Lock size={12} strokeWidth={2} />
                  <span>
                    <Trans>Isolated preview — no access to your account</Trans>
                  </span>
                </div>
              ) : null}
            </>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
