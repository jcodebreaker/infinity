import type { ThreadMessage } from "@superchat/contracts";
import {
  type TouchEvent,
  type UIEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
  type WheelEvent,
} from "react";
import { previewMessageText } from "../conversation/messageText";

/** The "Last:" line is cut to about this many characters. */
const LINE_CHARS = 60;
/** Scrolling on past Home's end this far (summed wheel deltaY), or swiping up this far, continues;
 * the same pull past the top of the Conversation goes back to Home. */
const PULL_PX = 140;
const SWIPE_PX = 90;
/** Wheel events closer together than this are one gesture (a flick and its momentum). */
const GESTURE_GAP_MS = 200;
/** How long the pull's "Home" tab stays after the wheel or finger stops. */
const PULL_LINGER_MS = 1500;
/** The slide between Home and the Conversation. */
const SLIDE_MS = 700;
const SLIDE_EASING = "cubic-bezier(.7,0,.2,1)";
const FADE_MS = 200;

/** Where the slide goes: up into the Conversation, or down back to Home. */
export type SlideWay = "up" | "down";

/** The first sentence of *text* (a reply's opening line). */
function firstSentence(text: string): string {
  const match = /^(.+?[.!?…])(\s|$)/u.exec(text);
  return match?.[1] ?? text;
}

/** *text* on one line, cut at a word to about `LINE_CHARS` with an ellipsis. */
export function clipLine(text: string, max = LINE_CHARS): string {
  const line = text.replace(/\s+/g, " ").trim();
  if (line.length <= max) return line;
  const cut = line.slice(0, max - 1);
  const space = cut.lastIndexOf(" ");
  return `${(space > max * 0.6 ? cut.slice(0, space) : cut).replace(/[\s,;:.–—-]+$/u, "")}…`;
}

/**
 * Home's "Last:" line: the Conversation's latest message with words in it, the person's
 * request as they wrote it or the first sentence of Super Chat's reply (Markdown stripped), on
 * one line; and when it was written. `null` while the Conversation has nothing to continue.
 */
export function lastLine(messages: readonly ThreadMessage[]): { text: string; at: string } | null {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (!message || (message.role !== "user" && message.role !== "bot")) continue;
    if (!message.blocks.some((block) => block.kind === "text" && block.text.trim())) continue;
    const text = previewMessageText(message);
    if (!text) continue;
    return {
      text: clipLine(message.role === "bot" ? firstSentence(text) : text),
      at: message.createdAt,
    };
  }
  return null;
}

/** Whether something between *target* and *root* (inclusive) can still scroll *way*. */
function scrollsFurther(
  target: EventTarget | null,
  root: HTMLElement,
  way: "down" | "up" = "down",
): boolean {
  let node = target instanceof Element ? target : null;
  while (node) {
    if (node instanceof HTMLElement && node.scrollHeight > node.clientHeight + 2) {
      const { overflowY } = getComputedStyle(node);
      const scrollable = overflowY === "auto" || overflowY === "scroll" || node === root;
      const further =
        way === "down"
          ? node.scrollTop + node.clientHeight < node.scrollHeight - 2
          : node.scrollTop > 2;
      if (scrollable && further) return true;
    }
    if (node === root) break;
    node = node.parentElement;
  }
  return false;
}

/**
 * Scrolling on past the end of Home (a wheel pull of 140px) or swiping up at its end calls
 * *onPull*; a list inside Home that still scrolls takes the wheel first. Spread the handlers on
 * Home's scroll root.
 */
export function usePullPastEnd(onPull: (() => void) | null) {
  const pulled = useRef(0);
  const touchY = useRef<number | null>(null);
  const onWheel = useCallback(
    (event: WheelEvent<HTMLElement>) => {
      if (!onPull || event.deltaY <= 0 || scrollsFurther(event.target, event.currentTarget)) {
        pulled.current = 0;
        return;
      }
      pulled.current += event.deltaY;
      if (pulled.current > PULL_PX) {
        pulled.current = 0;
        onPull();
      }
    },
    [onPull],
  );
  const onTouchStart = useCallback((event: TouchEvent<HTMLElement>) => {
    touchY.current = event.touches[0]?.clientY ?? null;
  }, []);
  const onTouchEnd = useCallback(
    (event: TouchEvent<HTMLElement>) => {
      const from = touchY.current;
      touchY.current = null;
      const to = event.changedTouches[0]?.clientY;
      if (!onPull || from === null || to === undefined || from - to <= SWIPE_PX) return;
      if (scrollsFurther(event.target, event.currentTarget)) return;
      onPull();
    },
    [onPull],
  );
  return { onWheel, onTouchStart, onTouchEnd };
}

/**
 * The Conversation's way back to Home, the mirror of `usePullPastEnd`: scrolling on up past its
 * top (a wheel pull of 140px) or swiping down there (90px) calls *onPull*, once nothing under the
 * pointer can still scroll up. `pull` (0 to 1) is how far the pull has gone, for the "Home" tab;
 * it lingers a moment after the gesture stops so the tab can be clicked. Spread `handlers` on an
 * element around the transcript.
 *
 * Earlier history is paged in by the transcript's "Load earlier messages" button, never by the
 * scroll itself, so the pull cannot steal a load. Still, while earlier messages can be loaded
 * (`dwellMs` > 0) a pull only counts when its gesture began after the top had been reached and
 * held that long: the flick that reaches the top stops there, on the button. `onPull` null (also
 * while an earlier page loads) turns the pull off.
 */
export function usePullPastStart(onPull: (() => void) | null, dwellMs = 0) {
  const [pull, setPull] = useState(0);
  const pulled = useRef(0);
  const topSince = useRef<number | null>(null);
  const gestureStart = useRef(0);
  const lastWheel = useRef(0);
  const touch = useRef<{ y: number; counts: boolean } | null>(null);
  const linger = useRef<number | undefined>(undefined);
  const show = useCallback((amount: number) => {
    window.clearTimeout(linger.current);
    setPull(Math.min(1, amount));
    if (amount > 0) linger.current = window.setTimeout(() => setPull(0), PULL_LINGER_MS);
  }, []);
  useEffect(() => () => window.clearTimeout(linger.current), []);
  useEffect(() => {
    if (!onPull) {
      pulled.current = 0;
      show(0);
    }
  }, [onPull, show]);
  /** At the top since when (now if it was not known yet); null while it can scroll up. */
  const atTop = useCallback((target: EventTarget | null, root: HTMLElement, now: number) => {
    if (scrollsFurther(target, root, "up")) {
      topSince.current = null;
      return null;
    }
    topSince.current ??= now;
    return topSince.current;
  }, []);
  const fire = useCallback(() => {
    pulled.current = 0;
    show(0);
    onPull?.();
  }, [onPull, show]);
  const onScrollCapture = useCallback(
    (event: UIEvent<HTMLElement>) => {
      atTop(event.target, event.currentTarget, performance.now());
    },
    [atTop],
  );
  const onWheel = useCallback(
    (event: WheelEvent<HTMLElement>) => {
      const now = performance.now();
      if (now - lastWheel.current > GESTURE_GAP_MS) gestureStart.current = now;
      lastWheel.current = now;
      const since = atTop(event.target, event.currentTarget, now);
      const early = dwellMs > 0 && since !== null && gestureStart.current < since + dwellMs;
      if (!onPull || event.deltaY >= 0 || since === null || early) {
        if (pulled.current) {
          pulled.current = 0;
          show(0);
        }
        return;
      }
      pulled.current -= event.deltaY;
      if (pulled.current > PULL_PX) fire();
      else show(pulled.current / PULL_PX);
    },
    [onPull, dwellMs, atTop, fire, show],
  );
  const onTouchStart = useCallback(
    (event: TouchEvent<HTMLElement>) => {
      const now = performance.now();
      const since = atTop(event.target, event.currentTarget, now);
      const y = event.touches[0]?.clientY;
      touch.current =
        y === undefined ? null : { y, counts: since !== null && now >= since + dwellMs };
    },
    [atTop, dwellMs],
  );
  const onTouchMove = useCallback(
    (event: TouchEvent<HTMLElement>) => {
      const from = touch.current;
      const to = event.touches[0]?.clientY;
      if (!onPull || !from?.counts || to === undefined) return;
      if (scrollsFurther(event.target, event.currentTarget, "up")) return;
      show(Math.max(0, to - from.y) / SWIPE_PX);
    },
    [onPull, show],
  );
  const onTouchEnd = useCallback(
    (event: TouchEvent<HTMLElement>) => {
      const from = touch.current;
      touch.current = null;
      const to = event.changedTouches[0]?.clientY;
      if (!onPull || !from?.counts || to === undefined || to - from.y <= SWIPE_PX) return;
      if (scrollsFurther(event.target, event.currentTarget, "up")) return;
      fire();
    },
    [onPull, fire],
  );
  return {
    pull: onPull ? pull : 0,
    handlers: { onScrollCapture, onWheel, onTouchStart, onTouchMove, onTouchEnd },
  };
}

/** Whether *target* is a text field with something in it, where ⌘↑ / ⌘↓ move the caret. */
function editingText(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement) {
    return target.value !== "";
  }
  if (target instanceof HTMLSelectElement) return true;
  return target.isContentEditable && Boolean(target.textContent?.trim());
}

/**
 * ⌘↑ (Ctrl+↑ off a Mac) calls *up* (back to Home) and ⌘↓ / Ctrl+↓ calls *down* (on into the
 * Conversation); either is null while it does not apply. A text field with something in it keeps
 * the keys for its caret, and an open dialog keeps them too.
 */
export function useSlideKeys(up: (() => void) | null, down: (() => void) | null) {
  useEffect(() => {
    if (!up && !down) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.altKey || event.shiftKey) return;
      if (!(event.metaKey || event.ctrlKey)) return;
      const go = event.key === "ArrowUp" ? up : event.key === "ArrowDown" ? down : null;
      if (!go || editingText(event.target)) return;
      if (event.target instanceof Element && event.target.closest('[role="dialog"]')) return;
      event.preventDefault();
      go();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [up, down]);
}

/**
 * Runs the slide on the two layers: up, Home leaves through the top while the Conversation
 * rises from below; down, the reverse. Under reduced motion it is a short crossfade of Home.
 * Resolves when it has played (at once without Web Animations); the returned `cancel` drops
 * the animations' held end state once the views have settled.
 */
export function slideViews(
  home: HTMLElement | null,
  chat: HTMLElement | null,
  way: SlideWay,
): { done: Promise<void>; cancel: () => void } {
  const animations: Animation[] = [];
  const play = (
    element: HTMLElement | null,
    frames: Keyframe[],
    duration: number,
    easing: string,
  ) => {
    if (!element || typeof element.animate !== "function") return;
    animations.push(element.animate(frames, { duration, easing, fill: "forwards" }));
  };
  const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  if (reduce) {
    const fade = [{ opacity: 1 }, { opacity: 0 }];
    play(home, way === "up" ? fade : [...fade].reverse(), FADE_MS, "ease");
  } else {
    const off = { up: "translateY(-100%)", down: "translateY(100%)" };
    const homeFrames = [{ transform: "none" }, { transform: off.up }];
    const chatFrames = [{ transform: off.down }, { transform: "none" }];
    play(home, way === "up" ? homeFrames : [...homeFrames].reverse(), SLIDE_MS, SLIDE_EASING);
    play(chat, way === "up" ? chatFrames : [...chatFrames].reverse(), SLIDE_MS, SLIDE_EASING);
  }
  return {
    done: Promise.all(animations.map((animation) => animation.finished)).then(
      () => undefined,
      () => undefined,
    ),
    cancel: () => {
      for (const animation of animations) animation.cancel();
    },
  };
}
