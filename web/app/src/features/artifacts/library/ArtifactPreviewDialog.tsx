import { useLingui } from "@lingui/react/macro";
import { cn, Dialog, DialogContent, DialogTitle } from "@superchat/ui-web";
import { useState } from "react";
import { ArtifactEditor } from "../editor/ArtifactEditor";
import { useArtifactPanelParts } from "../registry";
import { useArtifactContent } from "./useArtifactContent";

/**
 * A saved file in the artifact editor, in a floating dialog: where nothing frames it already (a
 * chat file card's Open). The Library opens files in its own sheet instead (`LibraryScreen`).
 */
export function ArtifactPreviewDialog({
  artifactId,
  onOpenChange,
}: {
  artifactId: string;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useLingui();
  const state = useArtifactContent(artifactId);
  const [fullScreen, setFullScreen] = useState(false);
  const ready = state.status === "ready" ? state : null;
  // Same registry parts as the side panel, so apps and sheets add their controls here too.
  const panel = useArtifactPanelParts({
    artifact: ready?.artifact,
    bytes: ready?.bytes,
    newestVersion: state.versions[0] ?? null,
  });

  return (
    <Dialog
      open
      onOpenChange={(open, details) => {
        // An overlay of the file's own (the Theme gallery, Publish) takes Escape first.
        if (!open && details?.reason === "escape-key" && panel.holdsEscape) {
          details.cancel();
          return;
        }
        onOpenChange(open);
      }}
    >
      <DialogContent
        showCloseButton={false}
        className={cn(
          "flex max-w-none flex-col gap-0 overflow-hidden p-0",
          fullScreen
            ? "h-screen w-screen sm:max-w-none sm:rounded-none"
            : "h-[90vh] w-[94vw] sm:max-w-6xl sm:rounded-3xl",
        )}
      >
        <DialogTitle className="sr-only">{ready ? ready.artifact.name : t`Loading…`}</DialogTitle>
        <ArtifactEditor
          content={state}
          panel={panel}
          onClose={() => onOpenChange(false)}
          fullScreen={{ on: fullScreen, toggle: () => setFullScreen((on) => !on) }}
        />
      </DialogContent>
    </Dialog>
  );
}
