import { useLingui } from "@lingui/react/macro";
import type { Topic, TopicCovered, TopicOpen } from "@superchat/contracts";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { rpc } from "../../lib/rpc";
import { daysAgo } from "./topicTime";

/** The topics' wire: `rpc.topics.list` / `rpc.topics.pick`. */
export type TopicsWire = {
  list: (input: { botId: string; days?: number }) => Promise<{ topics: Topic[] }>;
  pick: (input: { botId: string; key: string }) => Promise<unknown>;
};

export const LIVE_TOPICS_WIRE: TopicsWire = {
  list: (input) => rpc.topics.list(input),
  pick: (input) => rpc.topics.pick(input),
};

/** Re-read while the window is in view: a quiet moment writes topics on its own schedule. */
const TOPICS_POLL_MS = 5 * 60_000;

/** The person's topics this week (`null` until the first read), re-read on focus and on
 * `reload`. A `reload` while a read runs reads once more after it, so a write that landed
 * meanwhile is never missed. A failed read keeps what was shown. */
export function useTopics(botId: string | undefined, wire: TopicsWire = LIVE_TOPICS_WIRE) {
  const [topics, setTopics] = useState<Topic[] | null>(null);
  const reading = useRef(false);
  const again = useRef(false);
  const reload = useCallback(async () => {
    if (!botId) return;
    if (reading.current) {
      again.current = true;
      return;
    }
    reading.current = true;
    try {
      do {
        again.current = false;
        try {
          setTopics((await wire.list({ botId, days: 7 })).topics);
        } catch {
          setTopics((current) => current ?? []);
        }
      } while (again.current);
    } finally {
      reading.current = false;
    }
  }, [botId, wire]);
  useEffect(() => {
    setTopics(null);
    void reload();
    const onFocus = () => void reload();
    window.addEventListener("focus", onFocus);
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void reload();
    }, TOPICS_POLL_MS);
    return () => {
      window.removeEventListener("focus", onFocus);
      window.clearInterval(timer);
    };
  }, [reload]);
  return { topics, reload };
}

/** The words topics are shown with: when a topic was touched, a day heading, each kind tag. */
export function useTopicWords() {
  const { t, i18n } = useLingui();
  return useMemo(() => {
    const time = (at: number) => i18n.date(new Date(at * 1000), { timeStyle: "short" });
    const date = (at: number) =>
      i18n.date(new Date(at * 1000), { weekday: "short", day: "numeric", month: "short" });
    const day = (at: number, now: Date) => {
      const ago = daysAgo(at, now);
      return ago === 0 ? t`Today` : ago === 1 ? t`Yesterday` : date(at);
    };
    const kinds: Record<TopicCovered["kind"] | TopicOpen["kind"], string> = {
      Decided: t`Decided`,
      Made: t`Made`,
      Found: t`Found`,
      Asked: t`Asked`,
      Open: t`Open`,
      Promised: t`Promised`,
    };
    return {
      time,
      day,
      /** "Today 9:14", "Yesterday 16:05", "Tue 20 Oct". */
      when: (at: number, now: Date) =>
        daysAgo(at, now) <= 1 ? `${day(at, now)} ${time(at)}` : date(at),
      /** A covered line's `YYYY-MM-DD`, worded like a day heading. */
      coveredDay: (iso: string, now: Date) => {
        const at = new Date(`${iso}T12:00:00`).getTime() / 1000;
        return Number.isNaN(at) ? iso : day(at, now);
      },
      /** An open line's `YYYY-MM-DD` due day: "Due today", "Due tomorrow", "Due Fri 30 Oct". */
      dueDay: (iso: string, now: Date) => {
        const at = new Date(`${iso}T12:00:00`).getTime() / 1000;
        if (Number.isNaN(at)) return t`Due ${iso}`;
        const ago = daysAgo(at, now);
        const label = date(at);
        return ago === 0 ? t`Due today` : ago === -1 ? t`Due tomorrow` : t`Due ${label}`;
      },
      kinds,
      /** A tile's day: "Today", "Yesterday", "Fri" within the week, else "20 Oct". */
      tileDay: (at: number, now: Date) => {
        const ago = daysAgo(at, now);
        if (ago <= 0) return t`Today`;
        if (ago === 1) return t`Yesterday`;
        const when = new Date(at * 1000);
        return ago < 7
          ? i18n.date(when, { weekday: "short" })
          : i18n.date(when, { day: "numeric", month: "short" });
      },
      /** A promise's day in plain words, after its text: "due today", "due Friday",
       * "was due yesterday"; never a raw date. */
      dueWords: (iso: string, now: Date) => {
        const at = new Date(`${iso}T12:00:00`).getTime() / 1000;
        if (Number.isNaN(at)) return "";
        const ahead = -daysAgo(at, now);
        const when = new Date(at * 1000);
        const weekday = i18n.date(when, { weekday: "long" });
        const dayMonth = i18n.date(when, { day: "numeric", month: "short" });
        if (ahead === 0) return t`due today`;
        if (ahead === 1) return t`due tomorrow`;
        if (ahead === -1) return t`was due yesterday`;
        if (ahead > 1 && ahead < 7) return t`due ${weekday}`;
        if (ahead < -1 && ahead > -7) return t`was due ${weekday}`;
        return ahead < 0 ? t`was due ${dayMonth}` : t`due ${dayMonth}`;
      },
      /** The bold end of a feature tile whose promise is due: "It's due today." */
      dueSentence: (iso: string, now: Date) => {
        const at = new Date(`${iso}T12:00:00`).getTime() / 1000;
        if (Number.isNaN(at)) return "";
        const ahead = -daysAgo(at, now);
        const when = new Date(at * 1000);
        const weekday = i18n.date(when, { weekday: "long" });
        const dayMonth = i18n.date(when, { day: "numeric", month: "short" });
        if (ahead === 0) return t`It's due today.`;
        if (ahead === 1) return t`It's due tomorrow.`;
        if (ahead === -1) return t`It was due yesterday.`;
        if (ahead < -1 && ahead > -7) return t`It was due ${weekday}.`;
        return ahead < 0 ? t`It was due ${dayMonth}.` : t`It's due ${dayMonth}.`;
      },
    };
  }, [t, i18n]);
}
