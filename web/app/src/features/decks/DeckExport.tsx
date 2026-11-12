import type { Artifact } from "@superchat/contracts";
import { useCallback, useEffect, useRef, useState } from "react";
import { decodeArtifactBase64, downloadArtifactBytes } from "../../lib/artifact-open";
import { rpc } from "../../lib/rpc";

export type DeckExportFormat = "pptx" | "pdf";
export type ExportState =
  | { kind: "idle" }
  | { kind: "working"; format: DeckExportFormat }
  | { kind: "done"; format: DeckExportFormat; name: string }
  | { kind: "error"; format: DeckExportFormat };

/** How long the "added to your Library" confirmation stays on the Download button. */
export const EXPORT_DONE_MS = 3500;

/**
 * Export a deck and hand the result to the browser: the engine renders the file in the host
 * (it also lands in the Library as an artifact), then it is downloaded at once. One export at a
 * time; `cancel` abandons the wait.
 */
export function useDeckExport(deckId: string | undefined) {
  const [state, setState] = useState<ExportState>({ kind: "idle" });
  const run_ = useRef<AbortController | null>(null);
  const timer = useRef<number | undefined>(undefined);

  const reset = useCallback(() => {
    run_.current?.abort();
    run_.current = null;
    window.clearTimeout(timer.current);
    setState({ kind: "idle" });
  }, []);
  useEffect(() => reset, [deckId, reset]);

  const run = useCallback(
    async (format: DeckExportFormat) => {
      if (!deckId || run_.current) return;
      const controller = new AbortController();
      run_.current = controller;
      window.clearTimeout(timer.current);
      setState({ kind: "working", format });
      try {
        const file: Artifact = await rpc.decks.export(
          { artifactId: deckId, format },
          { signal: controller.signal },
        );
        const full = await rpc.artifacts.getById({ artifactId: file.id });
        if (controller.signal.aborted) return;
        downloadArtifactBytes(full.name, full.mimeType, decodeArtifactBase64(full.contentBase64));
        setState({ kind: "done", format, name: full.name });
        timer.current = window.setTimeout(() => setState({ kind: "idle" }), EXPORT_DONE_MS);
      } catch {
        if (!controller.signal.aborted) setState({ kind: "error", format });
      } finally {
        if (run_.current === controller) run_.current = null;
      }
    },
    [deckId],
  );

  const cancel = useCallback(() => {
    run_.current?.abort();
    run_.current = null;
    setState({ kind: "idle" });
  }, []);

  return { state, run, cancel };
}
