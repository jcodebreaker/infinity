import { plural } from "@lingui/core/macro";
import { deckSlideCount, isDeckArtifactName } from "@superchat/contracts";
import { type RefObject, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { SandboxedHtmlViewer } from "../../components/SandboxedHtmlViewer";
import { KIND_ICON } from "../../lib/artifact-kind";
import { decodeArtifactBase64 } from "../../lib/artifact-open";
import { rpc } from "../../lib/rpc";
import { postDeckNavigation, readDeckEvent } from "./deck-model";
import { pptxForPreview } from "./pptx-normalize";

/** The deck's own fit() leaves a 32px margin, so a 1952x1112 frame lays the stage out at 1:1. */
const FRAME_W = 1952;
const FRAME_H = 1112;
const MARGIN = 16;
const SLIDE_W = FRAME_W - 2 * MARGIN;
const SLIDE_H = FRAME_H - 2 * MARGIN;

/** Fetched decks by id and version, so a Library and a transcript share one download. */
const bytesCache = new Map<string, Promise<Uint8Array>>();

function deckBytes(id: string, version: number | undefined): Promise<Uint8Array> {
  const key = `${id}:${version ?? ""}`;
  let pending = bytesCache.get(key);
  if (!pending) {
    pending = rpc.artifacts
      .getById({ artifactId: id })
      .then((full) => decodeArtifactBase64(full.contentBase64));
    pending.catch(() => bytesCache.delete(key));
    bytesCache.set(key, pending);
  }
  return pending;
}

/** True once the element is near the viewport (never where IntersectionObserver is missing). */
function useNear() {
  const ref = useRef<HTMLDivElement>(null);
  const [near, setNear] = useState(false);
  useEffect(() => {
    const node = ref.current;
    if (!node || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setNear(true);
          observer.disconnect();
        }
      },
      { rootMargin: "200px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  return { ref, near };
}

/** Size of the element, for scaling a fixed-size frame to cover it. */
function useSize(ref: RefObject<HTMLDivElement | null>) {
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  useEffect(() => {
    const node = ref.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (rect?.width) setSize({ width: rect.width, height: rect.height });
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [ref]);
  return size;
}

/**
 * A deck's thumbnail: its first slide, drawn live once the card is near the screen. An HTML deck
 * renders in the same sandbox as its viewer; a .pptx through the same renderer as its preview.
 * The deck icon stands in until the slide is drawn, and stays when it cannot be. The slide
 * covers the card edge to edge whatever the card's shape. `onMeta` hears an HTML deck's
 * slide count ("4 slides") once its bytes are here.
 */
export function DeckCover({
  id,
  name,
  version,
  onMeta,
}: {
  id: string;
  name: string;
  version?: number;
  onMeta?: (meta: string) => void;
}) {
  const { ref, near } = useNear();
  const [bytes, setBytes] = useState<Uint8Array | null>(null);

  useEffect(() => {
    if (!near) return;
    let cancelled = false;
    deckBytes(id, version)
      .then((next) => {
        if (!cancelled) setBytes(next);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [near, id, version]);

  useEffect(() => {
    if (!bytes || !onMeta || !isDeckArtifactName(name)) return;
    const slides = deckSlideCount(new TextDecoder("utf-8").decode(bytes));
    if (slides) onMeta(plural(slides, { one: "# slide", other: "# slides" }));
  }, [bytes, name, onMeta]);

  const [drawn, setDrawn] = useState(false);
  const markDrawn = useCallback(() => setDrawn(true), []);
  const Icon = KIND_ICON.deck;
  return (
    <div ref={ref} className="pointer-events-none relative h-full w-full overflow-hidden">
      {drawn ? null : (
        <div className="absolute inset-0 grid place-items-center">
          <Icon size={36} strokeWidth={1.5} className="text-muted-foreground/50" />
        </div>
      )}
      {bytes ? (
        <div className={drawn ? "h-full w-full" : "h-full w-full opacity-0"}>
          {isDeckArtifactName(name) ? (
            <HtmlFirstSlide bytes={bytes} title={name} onDrawn={markDrawn} />
          ) : (
            <PptxFirstSlide bytes={bytes} onDrawn={markDrawn} />
          )}
        </div>
      ) : null}
    </div>
  );
}

function coverTransform(width: number, height: number): string {
  const scale = Math.max(width / SLIDE_W, height / SLIDE_H);
  const x = (width - SLIDE_W * scale) / 2;
  const y = (height - SLIDE_H * scale) / 2;
  return `translate(${x}px, ${y}px) scale(${scale}) translate(${-MARGIN}px, ${-MARGIN}px)`;
}

function HtmlFirstSlide({
  bytes,
  title,
  onDrawn,
}: {
  bytes: Uint8Array;
  title: string;
  onDrawn: () => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const frame = useRef<HTMLIFrameElement>(null);
  const size = useSize(box);
  const html = useMemo(() => new TextDecoder("utf-8").decode(bytes), [bytes]);
  // Like the viewer's rail: once the deck says it is ready, park it on its first slide.
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.source !== frame.current?.contentWindow) return;
      if (readDeckEvent(event.data)?.kind !== "ready") return;
      postDeckNavigation(frame.current?.contentWindow, { action: "go", index: 0 });
      onDrawn();
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [onDrawn]);
  return (
    <div ref={box} className="h-full w-full">
      {size ? (
        <div
          aria-hidden="true"
          className="origin-top-left"
          style={{
            width: FRAME_W,
            height: FRAME_H,
            // Crop the deck's own margin, then cover the card: scale to whichever side needs
            // more and center the overflow, so no band shows whatever the card's shape.
            transform: coverTransform(size.width, size.height),
          }}
        >
          <SandboxedHtmlViewer html={html} title={title} relay decorative frameRef={frame} />
        </div>
      ) : null}
    </div>
  );
}

function PptxFirstSlide({ bytes, onDrawn }: { bytes: Uint8Array; onDrawn: () => void }) {
  const host = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const container = host.current;
    if (!container) return;
    const abort = new AbortController();
    let viewer: { destroy(): void } | undefined;
    (async () => {
      const [{ PptxViewer, RECOMMENDED_ZIP_LIMITS }, source] = await Promise.all([
        import("@aiden0z/pptx-renderer"),
        pptxForPreview(bytes),
      ]);
      if (abort.signal.aborted) return;
      const opened = await PptxViewer.open(source, container, {
        renderMode: "slide",
        fitMode: "contain",
        zipLimits: RECOMMENDED_ZIP_LIMITS,
        lazySlides: true,
        signal: abort.signal,
      });
      if (abort.signal.aborted) {
        opened.destroy();
        return;
      }
      viewer = opened;
      onDrawn();
    })().catch(() => {
      // The card's own art shows through.
    });
    return () => {
      abort.abort();
      viewer?.destroy();
      container.replaceChildren();
    };
  }, [bytes, onDrawn]);

  return <div ref={host} aria-hidden="true" className="h-full w-full" />;
}
