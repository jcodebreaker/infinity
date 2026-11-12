import { expect, it } from "vitest";
import { describeSchedule } from "./schedule";

it("describes each plain choice", () => {
  expect(describeSchedule({ frequency: "daily", time: "09:00" })).toBe("Every day at 09:00");
  expect(describeSchedule({ frequency: "weekly", time: "08:30", weekday: 4 })).toBe(
    "Every Friday at 08:30",
  );
  expect(describeSchedule({ frequency: "monthly", time: "18:00", monthDay: 22 })).toBe(
    "Monthly on the 22nd at 18:00",
  );
  expect(describeSchedule({ frequency: "monthly", time: "18:00", monthDay: 11 })).toBe(
    "Monthly on the 11th at 18:00",
  );
  expect(describeSchedule(null)).toBe("Custom schedule");
});
