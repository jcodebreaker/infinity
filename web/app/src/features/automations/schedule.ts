import type { AutomationSchedule } from "@superchat/contracts";

/** Monday-first, matching the contract's `weekday`. */
export const WEEKDAYS = [
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
  "Sunday",
] as const;

function ordinal(day: number): string {
  const last = day % 10;
  const tens = day % 100;
  if (tens >= 11 && tens <= 13) return `${day}th`;
  if (last === 1) return `${day}st`;
  if (last === 2) return `${day}nd`;
  if (last === 3) return `${day}rd`;
  return `${day}th`;
}

/** "Every day at 09:00", "Every Monday at 09:00", "Monthly on the 1st at 09:00". */
export function describeSchedule(schedule: AutomationSchedule | null): string {
  if (!schedule) return "Custom schedule";
  if (schedule.frequency === "daily") return `Every day at ${schedule.time}`;
  if (schedule.frequency === "weekdays") return `Weekdays at ${schedule.time}`;
  if (schedule.frequency === "weekly") {
    return `Every ${WEEKDAYS[schedule.weekday ?? 0]} at ${schedule.time}`;
  }
  return `Monthly on the ${ordinal(schedule.monthDay ?? 1)} at ${schedule.time}`;
}
