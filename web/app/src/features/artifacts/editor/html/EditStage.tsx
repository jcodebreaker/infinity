// Portions modified from nexu-io/open-design apps/web/src/edit-mode/bridge.ts@802708f, Apache-2.0; changes: host side of the bridge: nonce plus event.source checks, zod-validated messages, double-buffered frame swap.
import { type DeckEditFromFrame, DeckEditFromFrameSchema } from "@superchat/contracts";
import { cn } from "@superchat/ui-web";
import { type MutableRefObject, useCallback, useEffect, useRef, useState } from "react";
import { SandboxedHtmlViewer } from "../../../../components/SandboxedHtmlViewer";
import { type BridgeMode, injectEditBridge, makeEditNonce } from "./bridge-script";

type Frame = { key: string; source: string; html: string; nonce: string };

const makeFrame = (source: string, mode: BridgeMode): Frame => {
  const nonce = makeEditNonce();
  return { key: nonce, source, html: injectEditBridge(source, nonce, mode), nonce };
};

/**
 * The file with the edit bridge (a deck, a page). A new source (a saved edit, superchat's change,
 * an undo) loads into a second, hidden frame first and replaces the visible one only when its
 * bridge reports ready, in the same place and with the same selection, so nothing flickers or
 * jumps. Messages count only
 * when they come from one of our frames' window AND carry that frame's nonce; the host posts the
 * same way.
 */
export function EditStage({
  html,
  title,
  mode = "deck",
  reloadKey,
  place,
  selectedIds,
  frameRef,
  onMessage,
  onReady,
  chipInset = 0,
  postRef,
}: {
  html: string;
  title: string;
  /** What the bridge selects: a deck's slide elements, or any element of a page. */
  mode?: BridgeMode;
  /** Bumped to reload the frame from the source (a preview that was not saved). */
  reloadKey: number;
  /** Puts a new frame where the person was (a deck: on the slide on screen). Called with the
   * frame's window once its bridge is ready, and with `foreign` for each message the frame sends
   * outside the edit protocol (a deck's own "ready"). */
  place?: (frame: Window | null | undefined, foreign?: unknown) => void;
  /** The ids to select again in a new frame. */
  selectedIds: MutableRefObject<string[]>;
  /** The frame the host navigates; follows the visible one. */
  frameRef: MutableRefObject<HTMLIFrameElement | null>;
  onMessage: (message: DeckEditFromFrame) => void;
  onReady?: () => void;
  /** How far in from the stage's left edge the selection chip stays (an overlapping drawer). */
  chipInset?: number;
  /** Receives the function that posts to the visible frame. */
  postRef: MutableRefObject<(message: Record<string, unknown>) => void>;
}) {
  const [frames, setFrames] = useState<Frame[]>(() => [makeFrame(html, mode)]);
  const [shownKey, setShownKey] = useState(() => frames[0]?.key ?? "");
  const elements = useRef(new Map<string, HTMLIFrameElement>());
  const wanted = useRef({ source: html, reloadKey });
  const placeRef = useRef(place);
  placeRef.current = place;
  const modeRef = useRef(mode);
  modeRef.current = mode;
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;
  const onReadyRef = useRef(onReady);
  onReadyRef.current = onReady;
  const chipInsetRef = useRef(chipInset);
  chipInsetRef.current = chipInset;

  // A different source (or a forced reload) queues a new hidden frame; only the newest waits.
  useEffect(() => {
    if (wanted.current.source === html && wanted.current.reloadKey === reloadKey) return;
    wanted.current = { source: html, reloadKey };
    const next = makeFrame(html, modeRef.current);
    setFrames((current) => [...current.filter((f) => f.key === shownKeyRef.current), next]);
  }, [html, reloadKey]);
  const shownKeyRef = useRef(shownKey);
  shownKeyRef.current = shownKey;

  const post = useCallback((key: string, message: Record<string, unknown>) => {
    const frame = framesRef.current.find((f) => f.key === key);
    const win = elements.current.get(key)?.contentWindow;
    if (!frame || !win) return;
    win.postMessage({ v: 1, nonce: frame.nonce, ...message }, "*");
  }, []);
  const framesRef = useRef(frames);
  framesRef.current = frames;

  useEffect(() => {
    postRef.current = (message) => post(shownKeyRef.current, message);
  }, [post, postRef]);

  useEffect(() => {
    const onWindowMessage = (event: MessageEvent) => {
      const entry = [...elements.current.entries()].find(
        ([, el]) => el.contentWindow === event.source,
      );
      if (!entry) return;
      const [key] = entry;
      const frame = framesRef.current.find((f) => f.key === key);
      if (!frame) return;
      const parsed = DeckEditFromFrameSchema.safeParse(event.data);
      if (!parsed.success || parsed.data.nonce !== frame.nonce) {
        // The file's own protocol (a deck's "ready"): the kind decides.
        placeRef.current?.(elements.current.get(key)?.contentWindow, event.data);
        return;
      }
      const message = parsed.data;
      if (message.type === "superchat:edit-ready") {
        placeRef.current?.(elements.current.get(key)?.contentWindow);
        post(key, { type: "superchat:edit-select", ids: selectedIds.current });
        post(key, { type: "superchat:edit-chip-inset", left: chipInsetRef.current });
        if (key !== shownKeyRef.current) {
          setShownKey(key);
          setFrames((current) => current.filter((f) => f.key === key));
        }
        onReadyRef.current?.();
      }
      if (key === shownKeyRef.current || message.type === "superchat:edit-ready") {
        onMessageRef.current(message);
      }
    };
    window.addEventListener("message", onWindowMessage);
    return () => window.removeEventListener("message", onWindowMessage);
  }, [post, selectedIds]);

  return (
    <div className="relative h-full w-full">
      {frames.map((frame) => (
        <div
          key={frame.key}
          data-testid={frame.key === shownKey ? "deck-edit-frame" : "deck-edit-frame-next"}
          className={cn(
            "absolute inset-0",
            frame.key !== shownKey && "pointer-events-none invisible",
          )}
        >
          <SandboxedHtmlViewer
            html={frame.html}
            title={title}
            relay
            frameRef={(el) => {
              if (el) {
                elements.current.set(frame.key, el);
                if (frame.key === shownKey || frames.length === 1) frameRef.current = el;
              } else elements.current.delete(frame.key);
            }}
          />
        </div>
      ))}
    </div>
  );
}
