import type { Activity, ActivityStep } from "@superchat/contracts";

/** Test fixtures for the Activity feed (a done turn by default; override what a case needs). */
export function activity(overrides: Partial<Activity> = {}): Activity {
  return {
    id: "act-1",
    kind: "turn",
    source: "turn",
    chatId: "chat-1",
    title: "Checked the budget totals",
    outcome: "Everything ties out.",
    summary: "Everything ties out.",
    status: "done",
    startedAt: "2026-10-03T11:21:00.000Z",
    finishedAt: "2026-10-03T11:22:00.000Z",
    date: "2026-10-03",
    ...overrides,
  };
}

export function step(title: string, n = 1): ActivityStep {
  return {
    itemId: `fc_${n}`,
    title,
    createdAt: "2026-10-03T11:21:30.000Z",
    tool: null,
  };
}

/** A Helper's Activity: `chat` is its own chat id; `parent` the chat that started it. */
export function helper(chat: string, overrides: Partial<Activity> = {}): Activity {
  return activity({
    id: `sub_agent:${chat}`,
    kind: "sub_agent",
    source: "background",
    chatId: chat,
    parentChatId: "conv_muse",
    title: `Helper ${chat}`,
    ...overrides,
  });
}
