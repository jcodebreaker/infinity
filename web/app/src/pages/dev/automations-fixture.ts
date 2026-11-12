import type { Automation, SystemJob } from "@superchat/contracts";

const HOUR = 3_600_000;
const now = Date.now();

/** Dev-only automations for `/dev/automations`; never sent anywhere. */
export const DEV_AUTOMATIONS: Automation[] = [
  {
    id: "auto-1",
    name: "Morning briefing",
    prompt: "Summarize my calendar and anything waiting on me.",
    rrule: "FREQ=DAILY;BYHOUR=8;BYMINUTE=30",
    schedule: { frequency: "daily", time: "08:30" },
    timezone: "UTC",
    enabled: true,
    nextRunAt: new Date(now + 5 * HOUR).toISOString(),
    lastRunAt: new Date(now - 19 * HOUR).toISOString(),
    lastRunStatus: "succeeded",
    model: null,
    createdAt: new Date(now - 90 * 24 * HOUR).toISOString(),
  },
  {
    id: "auto-2",
    name: "Weekly summary",
    prompt: "Write up what changed in my files this week.",
    rrule: "FREQ=WEEKLY;BYDAY=FR;BYHOUR=17;BYMINUTE=0",
    schedule: { frequency: "weekly", time: "17:00", weekday: 4 },
    timezone: "UTC",
    enabled: true,
    nextRunAt: new Date(now + 52 * HOUR).toISOString(),
    lastRunAt: new Date(now - 6 * 24 * HOUR).toISOString(),
    lastRunStatus: "failed",
    model: null,
    createdAt: new Date(now - 60 * 24 * HOUR).toISOString(),
  },
  {
    id: "auto-3",
    name: "Monthly report",
    prompt: "Draft the monthly report from my notes.",
    rrule: "FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=9;BYMINUTE=0",
    schedule: { frequency: "monthly", time: "09:00", monthDay: 1 },
    timezone: "UTC",
    enabled: false,
    nextRunAt: null,
    lastRunAt: null,
    lastRunStatus: null,
    model: null,
    createdAt: new Date(now - 10 * 24 * HOUR).toISOString(),
  },
];

/** Dev-only built-in jobs for `/dev/automations`. */
export const DEV_SYSTEM_JOBS: SystemJob[] = [
  {
    id: "memory_upkeep",
    name: "Memory upkeep",
    description:
      "Reads your recent conversations and keeps what is worth remembering about you up to date.",
    schedule: "Every hour, and after a conversation is condensed",
    lastRunAt: new Date(now - 40 * 60_000).toISOString(),
    lastOutcome: "Read 6 messages · saved 2 things about you",
    lastFailed: false,
    nextRunAt: new Date(now + 20 * 60_000).toISOString(),
    enabled: true,
  },
];
