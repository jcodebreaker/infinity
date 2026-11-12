import type {
  Artifact,
  ArtifactVersion,
  MessageBlock,
  StudioPrelude,
  ThreadMessage,
} from "@superchat/contracts";
import { useEffect, useMemo, useState } from "react";
import type { ArtifactContent } from "../../features/artifacts/editor/ArtifactEditor";
import sample from "../../features/artifacts/editor/html/sample-deck.txt?raw";
import { ArtifactRegistryProvider } from "../../features/artifacts/registry";
import { StudioView } from "../../features/artifacts/studio/ArtifactStudio";
import { decksExtension } from "../../features/decks/extension";

const BOT = "dev-bot";
const STUDIO = "dev-studio";
const NAME = "q3_review.deck.html";
const DAY = Date.UTC(2026, 9, 17, 10, 24) / 1000;
const at = (minutes: number) => new Date((DAY + minutes * 60) * 1000).toISOString();

/** The file's versions, newest first: made in Super Chat, changed in the studio and by hand. */
const VERSIONS: ArtifactVersion[] = [
  { id: "v6", version: 6, name: NAME, createdAt: at(40), origin: "ai", chatId: BOT },
  {
    id: "v5",
    version: 5,
    name: NAME,
    createdAt: at(31),
    origin: "manual",
    editSummary: "slide 1 cover-title color #1d2b4f→#c8553d",
  },
  {
    id: "v4",
    version: 4,
    name: NAME,
    createdAt: at(30),
    origin: "manual",
    editSummary: "slide 1 cover-title font-size 44px→52px",
  },
  {
    id: "v3",
    version: 3,
    name: NAME,
    createdAt: at(21),
    origin: "ai",
    chatId: STUDIO,
    itemId: "m2",
    fromStudio: true,
  },
  {
    id: "v2",
    version: 2,
    name: NAME,
    createdAt: at(11),
    origin: "ai",
    chatId: STUDIO,
    itemId: "m1",
    fromStudio: true,
  },
  { id: "v1", version: 1, name: NAME, createdAt: at(0), origin: "ai", chatId: BOT },
];

const said = (
  id: string,
  role: ThreadMessage["role"],
  text: string,
  minute: number,
): ThreadMessage => ({
  id,
  threadId: STUDIO,
  seq: minute,
  role,
  blocks: [{ kind: "text", text }],
  createdAt: at(minute),
});

/** The engine's notice for a studio runtime line (a hand edit, a save elsewhere). */
const noticed = (
  id: string,
  minute: number,
  notice: Omit<Extract<MessageBlock, { kind: "notice" }>, "kind">,
): ThreadMessage => ({
  id,
  threadId: STUDIO,
  seq: minute,
  role: "system",
  blocks: [{ kind: "notice", ...notice }],
  createdAt: at(minute),
});

const MESSAGES: ThreadMessage[] = [
  said("m1", "user", "Make the cover say what moved, not just faster.", 10),
  said("a1", "bot", "Done: the cover now reads “Deploys up 38%, incidents down”.", 12),
  said(
    "m2",
    "user",
    '<superchat-element-request file="q3_review.deck.html" artifact="v2" version="2" chip="Slide 1 · cover-lead">\n</superchat-element-request>\n\nShorter, one line.',
    20,
  ),
  said("a2", "bot", "Shortened the subtitle on slide 1 to one line.", 22),
  noticed("h4", 30, {
    notice: "hand_edit",
    version: 4,
    elementId: "cover-title",
    element: "the title",
    what: "You made the title larger",
  }),
  noticed("h5", 31, {
    notice: "hand_edit",
    version: 5,
    elementId: "cover-title",
    element: "the title",
    what: "You changed the colour of the title",
  }),
  noticed("n1", 41, { notice: "stale", version: 6, origin: "superchat" }),
];

/** What made v1 and v6 in Super Chat: the ask and the hand-back of the Rounds that saved them. */
const PRELUDE: StudioPrelude[] = [
  {
    version: 1,
    roundId: "r1",
    ask: "Make a 3-slide deck reviewing Q3 for the platform team.",
    handback:
      "I made your 3-slide Q3 review: a cover, the numbers (deploys up 38%, incidents down a fifth, p95 latency flat) and what is next for Q4.\n\nThe incident figure comes from the on-call log only, so treat it as approximate.",
    itemId: "c1",
    at: at(-1),
  },
  {
    version: 6,
    roundId: "r2",
    ask: "Add the Q4 hiring plan to the last slide.",
    handback: "Added the Q4 hiring plan to slide 3.",
    itemId: "c2",
    at: at(39),
  },
];

type Scenario = "history" | "running" | "queued" | "old";

/**
 * Dev-only route (`/dev/studio`, gated by `import.meta.env.DEV` in App.tsx): a file's studio on
 * fixtures, so it can be eyeballed and screenshotted without a session or engine.
 * `?scenario=history|running|queued|old` picks the state, `?theme=light|dark` forces a theme, and
 * `?phone=1` shows the page in a phone-sized frame (the toggle at the top), so the stacked
 * layout is the real one under 640px.
 */
export function StudioPreviewPage() {
  const params = new URLSearchParams(window.location.search);
  const scenario = (params.get("scenario") ?? "history") as Scenario;
  const framed = params.get("framed") === "1";
  const phone = params.get("phone") === "1";
  const [open, setOpen] = useState(true);
  useEffect(() => {
    const theme = params.get("theme");
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  });
  const bytes = useMemo(() => new TextEncoder().encode(sample), []);
  const shownId = scenario === "old" ? "v3" : "v6";
  const shown = VERSIONS.find((version) => version.id === shownId) as ArtifactVersion;
  const artifact: Artifact = {
    id: shown.id,
    botId: BOT,
    runId: null,
    name: NAME,
    description: "Q3 review",
    mimeType: "text/html",
    size: bytes.byteLength,
    version: shown.version,
    createdAt: shown.createdAt,
    groupId: "v1",
  };
  const content: ArtifactContent = {
    status: "ready",
    artifact,
    bytes,
    versions: VERSIONS,
    versionId: shownId,
    selectVersion: () => {},
    adoptVersion: () => {},
    newer: scenario === "old" ? (VERSIONS[0] ?? null) : null,
    showNewest: () => {},
    arrived: null,
  };
  if (phone) return <PhoneFrame params={params} />;
  return (
    <ArtifactRegistryProvider value={[decksExtension]}>
      {framed ? null : <PhoneToggle phone={false} />}
      <div className="flex h-dvh items-center justify-center bg-background text-muted-foreground">
        {open ? null : (
          <button type="button" onClick={() => setOpen(true)}>
            Open the studio
          </button>
        )}
      </div>
      {open ? (
        <div className="fixed inset-0 flex">
          <StudioView
            content={content}
            chat={{
              sessionId: STUDIO,
              messages: MESSAGES,
              prelude: PRELUDE,
              loaded: true,
              running: scenario === "running" || scenario === "queued",
              sent: () => {},
              failed: false,
              blockedOn: scenario === "queued" ? "Finishing another file first" : null,
            }}
            title="Q3 review"
            onClose={() => setOpen(false)}
          />
        </div>
      ) : null}
    </ArtifactRegistryProvider>
  );
}

/** The phone-width toggle, over the studio. */
function PhoneToggle({ phone }: { phone: boolean }) {
  const toggle = () => {
    const url = new URL(window.location.href);
    if (phone) url.searchParams.delete("phone");
    else url.searchParams.set("phone", "1");
    window.location.assign(url);
  };
  return (
    <button
      type="button"
      onClick={toggle}
      className="fixed top-3 start-1/2 z-70 -translate-x-1/2 rounded-full bg-foreground px-3 py-1.5 text-[12px] text-background shadow-float"
    >
      {phone ? "Full width" : "Phone width"}
    </button>
  );
}

/** This page again in a 390 × 844 frame, where the studio's phone layout applies for real. */
function PhoneFrame({ params }: { params: URLSearchParams }) {
  const inner = new URLSearchParams(params);
  inner.delete("phone");
  inner.set("framed", "1");
  return (
    <div className="flex h-dvh items-center justify-center bg-muted">
      <iframe
        title="Studio at phone width"
        src={`${window.location.pathname}?${inner.toString()}`}
        className="h-[844px] max-h-full w-[390px] rounded-[28px] border border-border bg-background"
      />
      <PhoneToggle phone />
    </div>
  );
}
