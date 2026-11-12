import { type ReactNode, useEffect, useRef, useState } from "react";
import { pptxForPreview } from "./pptx-normalize";

type State = "loading" | "ready" | "failed";

/**
 * Draws a .pptx the way PowerPoint lays it out (shapes, text, tables and native charts as DOM and
 * canvas, via the Apache-2.0 @aiden0z/pptx-renderer), one slide under the other, scaled to the
 * panel. Read-only: nothing is written back. When the file cannot be read the caller's `fallback`
 * shows instead; a preview is never approximated.
 */
export function PptxPreview({ bytes, fallback }: { bytes: Uint8Array; fallback: ReactNode }) {
  const host = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<State>("loading");

  useEffect(() => {
    const container = host.current;
    if (!container) return;
    const abort = new AbortController();
    let viewer: { destroy(): void } | undefined;
    setState("loading");
    (async () => {
      const [{ PptxViewer, RECOMMENDED_ZIP_LIMITS }, source] = await Promise.all([
        import("@aiden0z/pptx-renderer"),
        pptxForPreview(bytes),
      ]);
      if (abort.signal.aborted) return;
      const opened = await PptxViewer.open(source, container, {
        fitMode: "contain",
        zipLimits: RECOMMENDED_ZIP_LIMITS,
        listOptions: { windowed: true, initialSlides: 4, batchSize: 4 },
        signal: abort.signal,
      });
      if (abort.signal.aborted) {
        opened.destroy();
        return;
      }
      viewer = opened;
      setState("ready");
    })().catch(() => {
      if (!abort.signal.aborted) setState("failed");
    });
    return () => {
      abort.abort();
      viewer?.destroy();
      container.replaceChildren();
    };
  }, [bytes]);

  if (state === "failed") return fallback;
  return (
    <div
      ref={host}
      data-testid="pptx-preview"
      aria-busy={state === "loading"}
      className="h-full w-full overflow-auto bg-muted/40 p-4"
    />
  );
}
