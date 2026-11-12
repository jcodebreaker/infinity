import { useLingui } from "@lingui/react/macro";
import type { Topic } from "@superchat/contracts";
import { cn } from "@superchat/ui-web";
import { type CSSProperties, type ReactNode, useMemo, useState } from "react";
import {
  type FeatureTopic,
  featureTopic,
  firstSentences,
  openStatus,
  type TopicHues,
  topicHues,
  topicPalette,
  topicStatus,
  topicTag,
} from "./topicLook";
import { inRange, peakHeat, type TopicRange } from "./topicTime";
import { useTopicWords } from "./useTopics";

/** The two hues a tile's wash and tag are painted with, as the CSS reads them. */
function hueStyle({ h1, h2 }: TopicHues): CSSProperties {
  return { "--h1": h1, "--h2": h2 } as CSSProperties;
}

/** A topic's tag: a short name on a frosted pill, its dot in the topic's hue. `small` is the
 * flat, tinted version the morning panel uses beside a line. */
export function TopicTag({
  hues,
  label,
  small = false,
}: {
  /** The topic's pair (`topicHues(key, topicPalette(...))`), the same its tile shows. */
  hues: TopicHues;
  label: string;
  small?: boolean;
}) {
  return (
    <span className={cn("sc-tile-tag", small && "sc-tile-tag-sm")} style={hueStyle(hues)}>
      <i aria-hidden="true" />
      <span dir="auto">{label}</span>
    </span>
  );
}

/** The tag a topic of *topics* shows for *key*, or what its title gives when it is not listed. */
export function tagFor(topics: readonly Topic[] | null, key: string, title: string): string {
  const topic = topics?.find((candidate) => candidate.key === key);
  return topicTag(topic ?? { tag: null, title: title || key });
}

/**
 * Home's "Pick up where you left off" (ADR 0035, amended "Home tiles, status and tags"): one
 * feature tile for the topic that needs the person most (a draft prepared for them, else a
 * promise due), then a colour-washed tile per topic with its tag, full title, status and day.
 * The last 48 hours by default, this week on the toggle. Each tile marks itself `data-peak` so
 * the home's contour ground lifts under it. `after` (the morning panel) sits under the tiles.
 */
export function HomeTopics({
  topics,
  onPick,
  onReviewDraft,
  after,
}: {
  topics: readonly Topic[] | null;
  onPick: (topic: Topic, from: HTMLElement) => void;
  /** Opens the draft the Lookout prepared on a topic (its file, in the File Studio). */
  onReviewDraft?: (topic: Topic) => void;
  /** Shown under the tiles, e.g. the morning brief's panel (ADR 0037); pass it only when it
   * has something to show, so a Home with no topics and no brief has no section at all. */
  after?: ReactNode;
}) {
  const { t } = useLingui();
  const [range, setRange] = useState<TopicRange>("recent");
  const now = useMemo(() => new Date(), [topics]);
  if (!topics?.length && !after) return null;
  // No topics yet: only the morning panel.
  if (!topics?.length) return <section className="sc-topics flex w-full flex-col">{after}</section>;
  const shown = (topics ?? []).filter((topic) => inRange(topic, range, now));
  const feature = featureTopic(shown, now);
  const rest = feature ? shown.filter((topic) => topic.key !== feature.topic.key) : shown;
  // Over every listed topic, so a tile keeps its colour when the range changes and the morning
  // panel's tags match (the caller builds the same palette from the same list).
  const palette = topicPalette((topics ?? []).map((topic) => topic.key));
  const ranges: { key: TopicRange; label: string }[] = [
    { key: "recent", label: t`Last 48 hours` },
    { key: "week", label: t`This week` },
  ];
  return (
    <section aria-labelledby="home-topics-title" className="sc-topics flex w-full flex-col">
      <div className="flex flex-wrap items-baseline justify-between gap-3 px-1 pb-3.5">
        <h2
          id="home-topics-title"
          className="text-[20px] leading-tight font-semibold tracking-[-0.02em]"
        >
          {t`Pick up where you left off`}
        </h2>
        <fieldset
          aria-label={t`Range`}
          className="m-0 flex gap-0.5 rounded-full border-0 bg-foreground/7 p-[3px]"
        >
          {ranges.map((item) => (
            <button
              key={item.key}
              type="button"
              aria-pressed={range === item.key}
              onClick={() => setRange(item.key)}
              className={cn(
                "rounded-full px-[13px] py-1.5 text-[13px] font-medium text-ink-2 transition-colors focus-visible:outline-2 focus-visible:outline-ring",
                range === item.key && "bg-solid text-foreground shadow-sm",
              )}
            >
              {item.label}
            </button>
          ))}
        </fieldset>
      </div>
      {shown.length === 0 ? (
        <p className="px-1 text-[13.5px] text-ink-2">
          {range === "recent" ? t`Nothing in the last 48 hours.` : t`Nothing this week.`}
        </p>
      ) : (
        <div className="sc-tiles">
          {feature ? (
            <FeatureTile
              feature={feature}
              hues={topicHues(feature.topic.key, palette)}
              now={now}
              onPick={onPick}
              onReviewDraft={onReviewDraft}
            />
          ) : null}
          {rest.map((topic, index) => (
            <TopicTile
              key={topic.key}
              topic={topic}
              hues={topicHues(topic.key, palette)}
              // Beside the feature: the first row's small tiles.
              first={Boolean(feature) && index < 3}
              now={now}
              index={index + (feature ? 1 : 0)}
              onPick={onPick}
            />
          ))}
        </div>
      )}
      {after}
    </section>
  );
}

/** Staggered rise as Home opens (none under reduced motion). */
const RISE =
  "motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-bottom-4 motion-safe:fill-mode-both motion-safe:animation-duration-700";

function TopicTile({
  topic,
  hues,
  first,
  now,
  index,
  onPick,
}: {
  topic: Topic;
  hues: TopicHues;
  first: boolean;
  now: Date;
  index: number;
  onPick: (topic: Topic, from: HTMLElement) => void;
}) {
  const { t } = useLingui();
  const words = useTopicWords();
  const status = topicStatus(topic, now);
  // Where the work stands (the status line, else the next open line), and its due day if any.
  const statusText =
    status.kind === "done"
      ? t`Done. Nothing open`
      : status.due
        ? openStatus(status.text, words.dueWords(status.due, now))
        : status.text;
  return (
    <button
      type="button"
      data-testid="topic-card"
      data-peak={topic.key}
      data-peak-heat={peakHeat(topic, now)}
      aria-label={t`Pick up ${topic.title}`}
      onClick={(event) => onPick(topic, event.currentTarget)}
      style={{ ...hueStyle(hues), animationDelay: `${120 + index * 90}ms` }}
      className={cn("sc-tile", first && "sc-tile-first", RISE)}
    >
      <span className="sc-wash">
        <TopicTag hues={hues} label={topicTag(topic)} />
      </span>
      <span className="grid gap-1 px-1">
        <span className="sc-tile-title" dir="auto">
          {topic.title}
        </span>
        <span className={cn("sc-tile-status", status.needsYou && "sc-needs-you")} dir="auto">
          {statusText}
        </span>
        <span className="text-[13.5px] text-ink-2">{words.tileDay(topic.lastTouchedAt, now)}</span>
      </span>
    </button>
  );
}

function FeatureTile({
  feature,
  hues,
  now,
  onPick,
  onReviewDraft,
}: {
  feature: FeatureTopic;
  hues: TopicHues;
  now: Date;
  onPick: (topic: Topic, from: HTMLElement) => void;
  onReviewDraft?: (topic: Topic) => void;
}) {
  const { t } = useLingui();
  const words = useTopicWords();
  const { topic } = feature;
  const draft = feature.why === "prepared" && topic.prepared?.artifactId && onReviewDraft;
  const lead =
    feature.why === "prepared"
      ? firstSentences(topic.prepared?.line || topic.statusLine || topic.leftOff)
      : `${feature.due.line.text.replace(/[.\s]+$/, "")}.`;
  const bold = feature.due ? words.dueSentence(feature.due.due, now) : "";
  return (
    // The tile is an article, not a button: its two actions are the buttons ("Pick up" is the
    // keyboard path). A click elsewhere on it picks the topic up, like a plain tile.
    // biome-ignore lint/a11y/useKeyWithClickEvents: "Pick up" is the keyboard path
    <article
      data-testid="topic-feature"
      data-peak={topic.key}
      data-peak-heat={peakHeat(topic, now)}
      aria-labelledby={`topic-feature-${topic.key}`}
      onClick={(event) => {
        if ((event.target as HTMLElement).closest("button")) return;
        onPick(topic, event.currentTarget);
      }}
      style={{ ...hueStyle(hues), animationDelay: "120ms" }}
      className={cn("sc-tile sc-feature", RISE)}
    >
      <span className="sc-wash" aria-hidden="true" />
      <span className="sc-glow" aria-hidden="true" />
      <span className="sc-feature-body">
        <TopicTag hues={hues} label={topicTag(topic)} />
        <span id={`topic-feature-${topic.key}`} className="sc-feature-title" dir="auto">
          {topic.title}
        </span>
        <span className="sc-feature-text" dir="auto">
          {lead}
          {bold ? (
            <>
              {" "}
              <b>{bold}</b>
            </>
          ) : null}
        </span>
        <span className="mt-1.5 flex flex-wrap gap-2">
          {draft ? (
            <button
              type="button"
              className="sc-tile-btn sc-tile-btn-solid"
              onClick={() => onReviewDraft(topic)}
            >
              {t`Review the draft`}
            </button>
          ) : null}
          <button
            type="button"
            aria-label={t`Pick up ${topic.title}`}
            className={cn("sc-tile-btn", draft ? "sc-tile-btn-ghost" : "sc-tile-btn-solid")}
            onClick={(event) => {
              const tile = event.currentTarget.closest<HTMLElement>("[data-testid=topic-feature]");
              onPick(topic, tile ?? event.currentTarget);
            }}
          >
            {t`Pick up`}
          </button>
        </span>
      </span>
    </article>
  );
}
