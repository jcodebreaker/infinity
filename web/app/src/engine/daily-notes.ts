import {
  listOmnigentDailyNotes,
  type OmnigentDailyNote,
  putOmnigentDailyNote,
} from "@superchat/adapters/omnigent/browser";
import { engineClient, NO_EMAIL } from "./client";
import { onSuperChat, os } from "./context";

function mapDailyNote(note: OmnigentDailyNote) {
  return {
    date: note.date,
    sections: note.sections,
    editedByPerson: note.edited_by_person,
    finalized: note.finalized,
    updatedAt: note.updated_at,
  };
}

/** `dailyNotes.*`: the person's daily notes, relayed from the engine. */
export const dailyNotesRouter = {
  dailyNotes: {
    list: os.dailyNotes.list.handler(async ({ input }) => {
      const notes = await onSuperChat(
        listOmnigentDailyNotes(engineClient(), NO_EMAIL, input.limit),
      );
      return { notes: notes.map(mapDailyNote) };
    }),
    save: os.dailyNotes.save.handler(async ({ input }) =>
      mapDailyNote(
        await onSuperChat(
          putOmnigentDailyNote(engineClient(), NO_EMAIL, input.date, input.sections),
        ),
      ),
    ),
  },
};
