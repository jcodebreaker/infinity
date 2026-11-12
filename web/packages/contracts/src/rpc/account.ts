import { oc } from "@orpc/contract";
import * as z from "zod";
import { AvatarStyleSchema, MeSchema } from "../domain.js";
import { SearchQueryOutputSchema } from "../search.js";

export const accountContract = {
  me: oc.output(MeSchema),
  preferences: {
    // `timezone` goes to the engine (it runs scheduled work in the person's zone); the avatar
    // style is a preference of this browser.
    update: oc
      .input(
        z.object({ avatarStyle: AvatarStyleSchema.optional(), timezone: z.string().optional() }),
      )
      .output(MeSchema),
  },
  search: {
    query: oc.input(z.object({ q: z.string().max(200) })).output(SearchQueryOutputSchema),
  },
};
