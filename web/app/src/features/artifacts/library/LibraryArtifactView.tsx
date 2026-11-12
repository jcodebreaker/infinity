import { useLingui } from "@lingui/react/macro";
import { Button } from "@superchat/ui-web";
import { ChevronLeft } from "lucide-react";
import { useEffect } from "react";
import { ArtifactEditor } from "../editor/ArtifactEditor";
import { useArtifactPanelParts } from "../registry";
import { useArtifactContent } from "./useArtifactContent";

/**
 * A file opened from the Library, in the Library's own sheet: the artifact editor replaces the
 * grid in the same frame (one window, never a dialog over the sheet), with "‹ Library" to go back.
 */
export function LibraryArtifactView({
  artifactId,
  onBack,
  onClose,
  onHoldsEscape,
}: {
  artifactId: string;
  onBack: () => void;
  /** Closes the whole sheet. */
  onClose: () => void;
  /** Whether an overlay of the file's own (the Theme gallery, Publish) should get Escape. */
  onHoldsEscape?: (holds: boolean) => void;
}) {
  const { t } = useLingui();
  const state = useArtifactContent(artifactId);
  const ready = state.status === "ready" ? state : null;
  const panel = useArtifactPanelParts({
    artifact: ready?.artifact,
    bytes: ready?.bytes,
    newestVersion: state.versions[0] ?? null,
  });
  useEffect(() => onHoldsEscape?.(panel.holdsEscape), [onHoldsEscape, panel.holdsEscape]);
  return (
    <div data-testid="library-artifact" className="flex min-h-0 flex-1 flex-col">
      <ArtifactEditor
        content={state}
        panel={panel}
        onClose={onClose}
        leading={
          <Button
            variant="ghost"
            size="sm"
            data-testid="library-back"
            className="-ms-2 me-1 shrink-0 text-muted-foreground"
            onClick={onBack}
          >
            <ChevronLeft />
            {t`Library`}
          </Button>
        }
      />
    </div>
  );
}
