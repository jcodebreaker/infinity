import { cn } from "@superchat/ui-web";
import { useEffect, useRef, useState } from "react";
import { SandboxedHtmlViewer } from "../../components/SandboxedHtmlViewer";
import { postDeckNavigation, readDeckEvent } from "./deck-model";

/** The deck's own fit() leaves a 16px margin on each side, so a 1952x1112 frame lays the
 * 1920x1080 slide out at 1:1, 16px in from the frame's corner. */
const FRAME_W = 1952;
const FRAME_H = 1112;
const MARGIN = 16;
const SLIDE_W = 1920;
/** Every thumbnail is this wide and exactly 16:9, the slide edge to edge. */
export const THUMB_W = 120;
const THUMB_H = Math.round((THUMB_W * 9) / 16);
/** Without IntersectionObserver (tests) only the first few thumbnails mount. */
const FALLBACK_MOUNT = 6;

/** True while the element is near the viewport; thumbnails far from it do not hold a frame. */
function useNearViewport(index: number) {
  const ref = useRef<HTMLDivElement>(null);
  const [near, setNear] = useState(
    typeof IntersectionObserver === "undefined" && index < FALLBACK_MOUNT,
  );
  useEffect(() => {
    const node = ref.current;
    if (!node || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) setNear(entry.isIntersecting);
      },
      { rootMargin: "240px 0px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  return { ref, near };
}

/** One slide of the same sandboxed deck, live (not a screenshot), parked on its slide, with its
 * number beside it. */
export function DeckThumb({
  html,
  index,
  label,
  active,
  onSelect,
}: {
  html: string;
  index: number;
  label: string;
  active: boolean;
  onSelect: (index: number) => void;
}) {
  const scale = THUMB_W / SLIDE_W;
  const { ref, near } = useNearViewport(index);
  const frame = useRef<HTMLIFrameElement>(null);

  useEffect(() => {
    if (!near) return;
    const onMessage = (event: MessageEvent) => {
      if (event.source !== frame.current?.contentWindow) return;
      if (readDeckEvent(event.data)?.kind === "ready") {
        postDeckNavigation(frame.current?.contentWindow, { action: "go", index });
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [near, index]);

  useEffect(() => {
    if (active) ref.current?.scrollIntoView?.({ block: "nearest" });
  }, [active, ref]);

  return (
    <div ref={ref}>
      <button
        type="button"
        role="option"
        aria-selected={active}
        aria-current={active ? "true" : undefined}
        aria-label={label || String(index + 1)}
        title={label.replace(/^\d+\s*/, "") || undefined}
        onClick={() => onSelect(index)}
        className="group flex w-full items-start gap-2 rounded-lg px-1.5 py-1.5 text-start outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span
          className={cn(
            "w-4 shrink-0 pt-0.5 text-end text-[11.5px] tabular-nums",
            active ? "font-semibold text-foreground" : "text-muted-foreground",
          )}
        >
          {index + 1}
        </span>
        <span
          className={cn(
            "relative block shrink-0 overflow-hidden rounded-[5px] bg-muted outline motion-safe:transition-[outline-color]",
            active
              ? "outline-2 outline-offset-2 outline-foreground"
              : "outline-1 -outline-offset-1 outline-border group-hover:outline-foreground/40",
          )}
          style={{ width: THUMB_W, height: THUMB_H }}
        >
          {near ? (
            <span
              aria-hidden="true"
              className="pointer-events-none absolute start-0 top-0 block origin-top-left"
              style={{
                width: FRAME_W,
                height: FRAME_H,
                transform: `scale(${scale}) translate(-${MARGIN}px, -${MARGIN}px)`,
              }}
            >
              <SandboxedHtmlViewer html={html} title={label} relay decorative frameRef={frame} />
            </span>
          ) : null}
        </span>
      </button>
    </div>
  );
}
