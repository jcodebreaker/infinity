import { useRef } from "react";
import type { TranscriptWork, TurnWork } from "./workPlan";
import type { WorkState } from "./workState";
import type { StripHelper, StripTurn } from "./workStripState";

/** What one reply's strip is handed: its Helpers, the reply itself, and whether anything in the
 * Conversation runs (`lit`, only for a strip with Helpers that could be running). */
export interface TurnStrip {
  helpers: StripHelper[];
  turn: StripTurn;
  lit: boolean;
}

/** Every reply's strip by its anchor message, and the blocks the strips draw instead. */
export interface TurnStrips {
  strips: ReadonlyMap<string, TurnStrip>;
  drawn: ReadonlySet<string>;
  /** What each strip says, as text: the same text is the same strip. */
  keys: ReadonlyMap<string, string>;
}

const NO_STRIPS: TurnStrips = { strips: new Map(), drawn: new Set(), keys: new Map() };

function sameSet(a: ReadonlySet<string>, b: ReadonlySet<string>): boolean {
  if (a.size !== b.size) return false;
  for (const key of a) if (!b.has(key)) return false;
  return true;
}

/** One reply's strip from what the transcript says of it and the Conversation's work state. */
export function stripOf(
  found: TurnWork,
  work: WorkState,
  liveLabel: string | undefined,
): TurnStrip {
  const live = found.last && work.turnRunning;
  const outcome = found.last && work.cancelled ? "cancelled" : found.outcome;
  return {
    helpers: found.helpers,
    turn: {
      activityId: found.activityId,
      files: found.files,
      live,
      startedAt: found.startedAt,
      endedAt: found.endedAt,
      ...(outcome ? { outcome } : {}),
      ...(live && liveLabel ? { liveLabel } : {}),
    },
    lit: found.helpers.length > 0 && work.running,
  };
}

/**
 * Each reply's strip from the transcript's work and the Conversation's work state, keeping the
 * previous strip object (and drawn set) wherever nothing it says changed: a transcript update (a
 * streamed word, another reply's Helper) then re-renders only the messages whose strip it touched.
 */
export function nextTurnStrips(
  previous: TurnStrips,
  work: TranscriptWork,
  state: WorkState,
  liveLabel: string | undefined,
): TurnStrips {
  const strips = new Map<string, TurnStrip>();
  const keys = new Map<string, string>();
  for (const [id, found] of work.turns) {
    const strip = stripOf(found, state, liveLabel);
    const key = JSON.stringify(strip);
    const kept = previous.strips.get(id);
    keys.set(id, key);
    strips.set(id, kept && previous.keys.get(id) === key ? kept : strip);
  }
  const drawn = sameSet(previous.drawn, work.drawn) ? previous.drawn : work.drawn;
  return { strips, drawn, keys };
}

/** `nextTurnStrips` across renders. */
export function useTurnStrips(
  work: TranscriptWork,
  state: WorkState,
  liveLabel: string | undefined,
): TurnStrips {
  const last = useRef<TurnStrips>(NO_STRIPS);
  last.current = nextTurnStrips(last.current, work, state, liveLabel);
  return last.current;
}
