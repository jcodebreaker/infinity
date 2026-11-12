import { oc } from "@orpc/contract";
import * as z from "zod";
import { Id } from "../ids.js";

/** One day's note (engine daily notes). `sections` is keyed by the engine's section keys. */
const DailyNoteSchema = z.object({
  date: z.string(),
  sections: z.record(z.string(), z.string()),
  editedByPerson: z.boolean(),
  finalized: z.boolean(),
  updatedAt: z.number(),
});

export const dailyNotesContract = {
  dailyNotes: {
    /** The person's daily notes (engine `GET /v1/me/daily-notes`), newest first: one per local
     * day, sections keyed `talked_about` / `decisions` / `promised` / `open_loops`. */
    list: oc
      .input(z.object({ botId: Id, limit: z.number().int().min(1).max(60).default(14) }))
      .output(z.object({ notes: z.array(DailyNoteSchema) })),
    /** The person's edit of one day's note: given sections win and the engine's writers only
     * append to them afterwards. */
    save: oc
      .input(
        z.object({
          botId: Id,
          date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/),
          sections: z.record(z.string(), z.string().max(6000)),
        }),
      )
      .output(DailyNoteSchema),
  },
};
