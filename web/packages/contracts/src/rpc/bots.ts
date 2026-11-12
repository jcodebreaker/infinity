import { oc } from "@orpc/contract";
import * as z from "zod";
import { BotSchema } from "../domain.js";

/** The person's one Muse, as the app's Muse list shows it. */
export const botsContract = {
  bots: {
    list: oc.output(z.array(BotSchema)),
  },
};
