import { oc } from "@orpc/contract";
import * as z from "zod";
import { Id } from "../ids.js";

import { botId } from "./shared.js";

const VaultEntrySchema = z.object({
  id: z.string(),
  name: z.string(),
  site: z.string(),
  username: z.string(),
  createdAt: z.string(),
  lastUsedAt: z.string().nullable(),
});

const VaultRequestSchema = z.object({
  id: z.string(),
  name: z.string(),
  site: z.string(),
  reason: z.string(),
  status: z.enum(["pending", "saved", "expired"]),
});

export const vaultContract = {
  /** The person's secrets vault (engine `/v1/me/vault`): saved logins, metadata only. A value
   * is accepted once by `save` and never returned, logged or sent to the model. */
  vault: {
    list: oc.input(botId).output(z.object({ entries: z.array(VaultEntrySchema) })),
    remove: oc
      .input(z.object({ botId: Id, id: z.string() }))
      .output(z.object({ ok: z.literal(true) })),
    /** The secure-entry card's request (what the Muse asked for), by id. */
    request: oc.input(z.object({ botId: Id, requestId: z.string() })).output(VaultRequestSchema),
    /** The secure-entry form's submit; saves the login and, with `requestId`, closes the card. */
    save: oc
      .input(
        z.object({
          botId: Id,
          requestId: z.string().optional(),
          name: z.string().min(1).max(128),
          site: z.string().min(1).max(512),
          username: z.string().max(256).optional(),
          password: z.string().min(1).max(4096),
        }),
      )
      .output(VaultEntrySchema),
  },
};
