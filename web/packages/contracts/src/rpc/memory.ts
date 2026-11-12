import { oc } from "@orpc/contract";
import * as z from "zod";
import { Id } from "../ids.js";

/** One memory claim for the Memory tab's editable sections (engine `memory/claims`). `origin`
 * is where it came from: the person's words, their own edit, or something Superchat noticed. */
const MemoryClaimSchema = z.object({
  id: z.string(),
  kind: z.string(),
  text: z.string(),
  origin: z.enum(["said", "edited", "noticed"]),
  personAuthored: z.boolean(),
  /** Epoch seconds the claim was last confirmed (first written when never reinforced). */
  date: z.number(),
  /** Past its end date: kept out of Superchat's context and search, shown so the person can forget it. */
  expired: z.boolean(),
});

export const memoryContract = {
  memory: {
    /** The Muse's Memory Profile from Omnigent (docs/context.md): what it carries
     * into every turn about the person. `null` when nothing is remembered yet. */
    profile: oc.input(z.object({ botId: Id })).output(z.object({ profile: z.string().nullable() })),
    /** Active memory claims for the Memory tab's sections (engine `GET .../memory/claims`),
     * newest first; each carries its kind, source and date. */
    claims: oc
      .input(z.object({ botId: Id }))
      .output(z.object({ claims: z.array(MemoryClaimSchema) })),
    /** The person's text edit of one claim: it becomes person-authored (upkeep and Helpers
     * never overwrite it). */
    editClaim: oc
      .input(z.object({ botId: Id, claimId: z.string(), text: z.string().min(1).max(2000) }))
      .output(MemoryClaimSchema),
    /** The person deletes one claim. */
    forgetClaim: oc
      .input(z.object({ botId: Id, claimId: z.string() }))
      .output(z.object({ ok: z.literal(true) })),
  },
};
