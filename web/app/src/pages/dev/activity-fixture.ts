import type { Activity, ActivityStep, ThreadMessage } from "@superchat/contracts";

// Fixture data for /dev/activity (ActivityPreviewPage.tsx): the Activity panel on in-memory
// data, so the panel's grouping, live line, and run page can be reviewed without the
// Omnigent backend (`activities.*` isn't served by the local dev API). Steps use real engine tool names and realistic
// payloads (engine/omnigent/tools/builtins/) so the icon + title + snippet registry
// (web/packages/core/src/tool-presentation.ts) has something true to life to present.

const now = Date.now();
const minutesAgo = (minutes: number) => new Date(now - minutes * 60_000).toISOString();
const hoursAgo = (hours: number) => new Date(now - hours * 60 * 60_000).toISOString();
const daysAgo = (days: number) => new Date(now - days * 24 * 60 * 60_000).toISOString();

function dateOf(iso: string): string {
  return iso.slice(0, 10);
}

export const DEV_ACTIVITY_BOT_ID = "dev-activity-muse";

/** Fixture for the Memory tab (MemoryTab.tsx / `rpc.memory.profile`'s shape). */
export const DEV_MEMORY_PROFILE = [
  "[Standing memory about the user — provided by the system, not a message from the user]",
  "",
  "Preferences:",
  "- Prefers phone-first slide decks",
  "- Weekly branch KPI summary on Monday mornings",
  "",
  "About the user:",
  "- Works branch operations at a bank",
  "- Reviews the Q3 steering deck with Dana",
  "",
  "[End of standing memory]",
].join("\n");

const HELPER_STARTED = minutesAgo(4);
const HELPER_CHAT_ID = "helper-1";

/** A tool call's detail in the engine's own shape (engine/omnigent/tools/builtins/
 * spawn.py `_project_activity_item`/`_step_detail`): the call's JSON-encoded arguments
 * string, and the result's raw output string (often itself JSON-encoded). */
function toolDetail(tool: string, args: Record<string, unknown>, content: string) {
  return {
    call: { role: "assistant", type: "tool_call", name: tool, args: JSON.stringify(args) },
    result: { role: "tool", type: "tool_result", name: tool, content },
  };
}

const HELPER_STEPS: ActivityStep[] = [
  {
    itemId: "step-1",
    title: "Searched memory for 'Q3 budget totals'",
    createdAt: minutesAgo(4),
    tool: "memory_search",
    detail: toolDetail(
      "memory_search",
      { query: "Q3 budget totals" },
      JSON.stringify({
        results: [{ claim_id: "c1", text: "Dana owns the Q3 budget review", kind: "fact" }],
      }),
    ),
  },
  {
    itemId: "step-2",
    title: "Sent a message to sub-agent 'Q3 budget check'",
    createdAt: minutesAgo(1),
    tool: "sys_session_send",
    detail: toolDetail(
      "sys_session_send",
      {
        agent: "analyst",
        title: "Q3 budget check",
        args: "Compare the Q3 ledger against the budget sheet.",
      },
      JSON.stringify({
        task_id: "task_1",
        kind: "sub_agent",
        agent: "analyst",
        title: "Q3 budget check",
        conversation_id: HELPER_CHAT_ID,
        status: "in_progress",
        message: "Dispatched; result will arrive in the inbox.",
      }),
    ),
  },
];

const DONE_TURN_STEPS: ActivityStep[] = [
  {
    itemId: "step-a",
    title: "Read earlier messages",
    createdAt: minutesAgo(40),
    tool: "session_history",
    detail: toolDetail(
      "session_history",
      { action: "read" },
      JSON.stringify({
        turns: [
          { messages: [{ role: "user", type: "text", content: "What did we decide last week?" }] },
        ],
        next_cursor: null,
      }),
    ),
  },
  {
    itemId: "step-b",
    title: "Used web_search",
    createdAt: minutesAgo(39),
    tool: "web_search",
    detail: toolDetail(
      "web_search",
      { query: "phone-first slide deck design tips" },
      [
        "Short decks read best as single points per slide.",
        "",
        "1. Designing decks for phones\n   https://blog.example.com/phone-decks\n   One idea per slide, large type.",
        "2. Steering review best practices\n   https://www.investopedia.com/steering-review\n   Keep it to one page per topic.",
      ].join("\n"),
    ),
  },
  {
    itemId: "step-c",
    title: "Drafted 4 slides sized for a phone screen",
    createdAt: minutesAgo(38),
    tool: "write_file",
    detail: toolDetail("write_file", { file: "steering-review.pdf" }, "Saved steering-review.pdf"),
  },
];

// The work strip under a reply (WorkStrip.tsx): a deck Helper that has reported progress and
// raised a flag, and a reading Helper that finished in 2:14.
const DECK_STARTED = minutesAgo(1.2);
export const DECK_HELPER_CHAT_ID = "helper-deck";
export const READ_HELPER_CHAT_ID = "helper-read";

export const INITIAL_ACTIVITIES: Activity[] = [
  {
    id: "act-helper-deck",
    kind: "sub_agent",
    source: "background",
    chatId: DECK_HELPER_CHAT_ID,
    parentChatId: "conv-1",
    title: "Write the Q3 lending deck",
    outcome: null,
    summary: null,
    status: "in_progress",
    startedAt: DECK_STARTED,
    finishedAt: null,
    date: dateOf(DECK_STARTED),
    steps: [
      {
        itemId: "deck-1",
        title: "Read the Q2 review for its structure",
        createdAt: minutesAgo(1),
        tool: "report_progress",
      },
      {
        itemId: "deck-2",
        title: "Flagged: Ledger says 6.1% loan growth, the memo says 5.4%; using the ledger",
        createdAt: minutesAgo(0.8),
        tool: "raise_flag",
      },
      {
        itemId: "deck-3",
        title: "Slide 6 of 10 written",
        createdAt: minutesAgo(0.2),
        tool: "report_progress",
      },
    ],
  },
  {
    id: "act-helper-read",
    kind: "sub_agent",
    source: "background",
    chatId: READ_HELPER_CHAT_ID,
    parentChatId: "conv-1",
    title: "Read the three files you dropped",
    outcome: "Read the ledger, the memo and the provisions sheet.",
    summary: "Read the ledger, the memo and the provisions sheet.",
    status: "done",
    startedAt: minutesAgo(30),
    finishedAt: new Date(now - 30 * 60_000 + 134_000).toISOString(),
    date: dateOf(minutesAgo(30)),
  },
  {
    id: "act-helper-running",
    kind: "sub_agent",
    source: "background",
    chatId: HELPER_CHAT_ID,
    title: "Check the Q3 budget totals",
    outcome: "Comparing line 61 of 84.",
    summary: "Comparing line 61 of 84.",
    status: "in_progress",
    startedAt: HELPER_STARTED,
    finishedAt: null,
    date: dateOf(HELPER_STARTED),
    // Present on the list summary too (not just `activities.get`) so the row's live line
    // can show the latest Step's icon + title while this Activity is still in progress.
    steps: HELPER_STEPS,
  },
  {
    id: "act-turn-drafted",
    kind: "turn",
    source: "turn",
    chatId: "conv-1",
    title: "Drafted the steering review",
    outcome: "4 slides, phone-first, ready for edits.",
    summary: "4 slides, phone-first, ready for edits.",
    status: "done",
    startedAt: minutesAgo(40),
    finishedAt: minutesAgo(38),
    date: dateOf(minutesAgo(40)),
  },
  {
    // A turn in a file's studio (ADR 0024): listed under the file's name, never as a Helper.
    id: "turn:dev-studio:resp_bar",
    kind: "turn",
    source: "studio",
    chatId: "dev-studio",
    artifact: { groupId: "dev-q3-review", name: "q3_review.deck.html" },
    title: "Make the regions a bar chart, biggest first",
    outcome: "Slide 3 is a bar chart, largest region first.",
    summary: "Slide 3 is a bar chart, largest region first.",
    status: "done",
    startedAt: minutesAgo(50),
    finishedAt: minutesAgo(49),
    date: dateOf(minutesAgo(50)),
  },
  {
    id: "act-turn-failed",
    kind: "turn",
    source: "turn",
    chatId: "conv-1",
    title: "Retrieve idea details",
    outcome: "The ideas store didn't answer.",
    summary: "The ideas store didn't answer.",
    status: "failed",
    startedAt: hoursAgo(5),
    finishedAt: hoursAgo(5),
    date: dateOf(hoursAgo(5)),
  },
  {
    id: "act-helper-done-yesterday",
    kind: "sub_agent",
    source: "scheduled",
    chatId: "helper-2",
    title: "Summarize today's headlines",
    outcome: "A 5-point summary of today's headlines.",
    summary: "A 5-point summary of today's headlines.",
    status: "done",
    startedAt: daysAgo(1),
    finishedAt: daysAgo(1),
    date: dateOf(daysAgo(1)),
  },
  {
    id: "act-turn-cancelled",
    kind: "turn",
    source: "turn",
    chatId: "conv-1",
    title: "Pull last quarter's branch KPIs",
    outcome: null,
    summary: null,
    status: "cancelled",
    startedAt: daysAgo(1),
    finishedAt: daysAgo(1),
    date: dateOf(daysAgo(1)),
  },
  {
    id: "act-turn-older",
    kind: "turn",
    source: "turn",
    chatId: "conv-1",
    title: "Tightened the new-accounts welcome email",
    outcome: "Shortened it to three lines.",
    summary: "Shortened it to three lines.",
    status: "done",
    startedAt: daysAgo(6),
    finishedAt: daysAgo(6),
    date: dateOf(daysAgo(6)),
  },
];

/** A second, older page for "Load earlier" — returned once `before` is older than
 * every Activity above. */
export const EARLIER_ACTIVITIES: Activity[] = [
  {
    id: "act-turn-earliest",
    kind: "turn",
    source: "scheduled",
    chatId: "conv-1",
    title: "Set up the weekly KPI routine",
    outcome: "Scheduled for Monday mornings.",
    summary: "Scheduled for Monday mornings.",
    status: "done",
    startedAt: daysAgo(12),
    finishedAt: daysAgo(12),
    date: dateOf(daysAgo(12)),
  },
];

/** The deck Helper's full detail: its progress lines head the tool steps after them, so the run
 * page reads as a story (ActivityRunDialog.tsx `StepsTimeline`). */
function deckStep(
  n: number,
  minutes: number,
  tool: string,
  args: Record<string, unknown>,
  title = "",
) {
  return {
    itemId: `deck-story-${n}`,
    title,
    createdAt: minutesAgo(minutes),
    tool,
    detail: toolDetail(tool, args, "ok"),
  };
}

const DECK_STORY_STEPS: ActivityStep[] = [
  deckStep(1, 1.2, "sys_os_shell", { command: "ls -la /work/inputs" }),
  deckStep(2, 1.15, "sys_os_shell", { command: "pip install python-pptx" }),
  deckStep(3, 1, "report_progress", {}, "Read the Q2 review for its structure"),
  deckStep(4, 0.95, "sys_os_read", { path: "/work/inputs/q2-review.pptx" }),
  deckStep(5, 0.9, "sys_os_shell", { command: "python3 /work/extract_outline.py q2-review.pptx" }),
  deckStep(
    6,
    0.8,
    "raise_flag",
    {},
    "Flagged: Ledger says 6.1% loan growth, the memo says 5.4%; using the ledger",
  ),
  deckStep(7, 0.6, "sys_os_write", { path: "/work/build_deck.py" }),
  deckStep(8, 0.5, "sys_os_shell", { command: "cd /work && python3 build_deck.py --slides 1-6" }),
  deckStep(9, 0.45, "artifact_save", { title: "Q3 lending deck", path: "/work/q3.pptx" }),
  deckStep(10, 0.2, "report_progress", {}, "Slide 6 of 10 written"),
  deckStep(11, 0.15, "sys_os_edit", { path: "/work/build_deck.py" }),
  deckStep(12, 0.1, "sys_os_shell", { command: "soffice --headless --convert-to pdf q3.pptx" }),
];

const DETAIL_BY_ID: Record<string, ActivityStep[]> = {
  "act-helper-deck": DECK_STORY_STEPS,
  "act-helper-running": HELPER_STEPS,
  "act-turn-drafted": DONE_TURN_STEPS,
};

export function activityDetail(activityId: string): ActivityStep[] {
  return DETAIL_BY_ID[activityId] ?? [];
}

export const HELPER_MESSAGES: Record<string, ThreadMessage[]> = {
  [HELPER_CHAT_ID]: [
    {
      id: "helper-m1",
      threadId: HELPER_CHAT_ID,
      seq: 1,
      role: "user",
      blocks: [
        {
          kind: "text",
          text: "Compare the Q3 ledger against the budget sheet, line by line, and flag anything that differs.",
        },
      ],
      createdAt: HELPER_STARTED,
    },
    {
      id: "helper-m2",
      threadId: HELPER_CHAT_ID,
      seq: 2,
      role: "bot",
      blocks: [
        {
          kind: "text",
          text: "Opened both files and started matching 84 line items. 61 are done so far; everything has tied out except Vendor Services, which I'm still checking.",
        },
      ],
      createdAt: minutesAgo(1),
    },
  ],
};

// The deck reply as the transcript holds it (the person's message, the reply's text with its
// Helper), read by the real `transcriptWork`, and its Helper at each stage of its work.

/** What the deck Helper has reported at each stage. */
const DECK_PROGRESS = [
  "Reading 8 tickers across 3 regions",
  "Europe 38.3%, North America 37.1%, Asia 24.7%",
  "Slide 3 of 6: volume share by region",
  "Date note added to slide 5",
  "48 figures checked, all match the file",
];
const DECK_FLAG = "The data ends on 16 October, not 20 October as the file name says.";

export const DECK_STAGES = DECK_PROGRESS.length;
const MARKETS_HELPER_CHAT_ID = "helper-markets-deck";
const DECK_ASKED = minutesAgo(1.4);

function ask(id: string, text: string, at: string): ThreadMessage {
  return {
    id,
    threadId: "conv-1",
    seq: 0,
    role: "user",
    blocks: [{ kind: "text", text }],
    createdAt: at,
  };
}

/** The deck reply, with its Helper. */
export function deckMessages(): ThreadMessage[] {
  return [
    ask(
      "ask-deck",
      "Make a presentation from mock_markets.csv: volume by region and the trend, about six slides with charts I can edit.",
      DECK_ASKED,
    ),
    {
      id: "reply-deck",
      threadId: "conv-1",
      seq: 9,
      role: "bot",
      blocks: [
        {
          kind: "text",
          text: "I'll read the CSV, work out each region's share and the trend, and build a six-slide deck with charts you can edit.",
        },
        {
          kind: "helper",
          helperId: MARKETS_HELPER_CHAT_ID,
          title: "Build the markets deck",
          status: "in_progress",
        },
      ],
      createdAt: DECK_ASKED,
    },
  ];
}

/** The deck Helper as the feed lists it at `stage`: running until the last. */
export function deckHelper(stage: number): Activity {
  const done = stage >= DECK_STAGES;
  const steps: ActivityStep[] = DECK_PROGRESS.slice(0, Math.max(1, stage)).map((title, index) => ({
    itemId: `markets-deck-${index}`,
    title,
    createdAt: minutesAgo(1.3 - index * 0.2),
    tool: "report_progress",
  }));
  if (stage >= 3) {
    steps.splice(2, 0, {
      itemId: "markets-deck-flag",
      title: `Flagged: ${DECK_FLAG}`,
      createdAt: minutesAgo(0.8),
      tool: "raise_flag",
    });
  }
  return {
    id: "act-helper-markets-deck",
    kind: "sub_agent",
    source: "background",
    chatId: MARKETS_HELPER_CHAT_ID,
    parentChatId: "conv-1",
    title: "Build the markets deck",
    outcome: done ? "Six slides, every chart editable." : null,
    summary: done ? "Six slides, every chart editable." : null,
    status: done ? "done" : "in_progress",
    startedAt: minutesAgo(1.35),
    finishedAt: done ? new Date(Date.parse(minutesAgo(1.35)) + 101_000).toISOString() : null,
    date: dateOf(DECK_ASKED),
    steps,
  };
}
