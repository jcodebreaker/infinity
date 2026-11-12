import * as z from "zod";

// Shapes shared by the Muse edition: Proactivity and Muse settings, the Muse's state and its
// starting profile. Words follow docs/glossary.md. The Goal, Ask, Approval, Post, Followed topic, Idea
// and chat shapes live in rpc/<capability>.ts and are re-exported from the package index.

export const PROACTIVITY_LEVELS = ["off", "low", "normal", "high"] as const;
export const ProactivitySchema = z.enum(PROACTIVITY_LEVELS);
export type Proactivity = z.infer<typeof ProactivitySchema>;

/** Local-time window with no background work, "HH:MM-HH:MM"; may wrap midnight. */
export const QuietHoursSchema = z
  .string()
  .regex(/^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$/);

export const MuseSettingsSchema = z.object({
  proactivity: ProactivitySchema,
  quietHours: QuietHoursSchema.nullable(),
});
export type MuseSettings = z.infer<typeof MuseSettingsSchema>;

export const DEFAULT_MUSE_SETTINGS: MuseSettings = {
  proactivity: "normal",
  quietHours: "22:00-08:00",
};

/** Default identity color of a new Muse (sky). */
export const DEFAULT_MUSE_COLOR = "#0090FF";

/** Default name of a new Muse. */
export const DEFAULT_MUSE_NAME = "superchat";

/** What the Muse's face shows: resting, thinking, working, or waiting on the person. */
export const MuseStateSchema = z.enum(["idle", "thinking", "working", "waiting"]);
export type MuseState = z.infer<typeof MuseStateSchema>;

/**
 * The profile a new Muse is created with, so it knows who it is from the first message
 * instead of reporting a blank role.
 */
export function museBotProfile(museName: string, personName: string) {
  const person = personName.trim() || "the person you work for";
  return {
    title: "AI teammate",
    description: `${person}'s AI teammate — already on it.`,
    instructions: [
      `You are ${museName}, ${person}'s AI teammate. They work at a bank, and you work alongside them on their everyday work.`,
      "Prepare meetings and briefings, research and pull the numbers, draft emails and documents, track follow-ups, and keep their Goals moving — in the background, without being asked.",
      "Be proactive: when you see the next useful step, take it or offer it. Ask before anything that can't be undone, such as sending, submitting, booking, or paying.",
      "Write plainly and briefly, in the first person. Never invent figures; say where numbers come from. Do not give personal investment advice.",
    ].join("\n"),
  };
}
