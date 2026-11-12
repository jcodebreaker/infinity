import {
  forgetOmnigentMemoryClaim,
  getOmnigentMemoryProfile,
  listOmnigentMemoryClaims,
  type OmnigentMemoryClaim,
  patchOmnigentMemoryClaim,
} from "@superchat/adapters/omnigent/browser";
import { engineClient, museSessionId, NO_EMAIL } from "./client";
import { onSuperChat, os } from "./context";

function mapClaim(claim: OmnigentMemoryClaim) {
  return {
    id: claim.claim_id,
    kind: claim.kind,
    text: claim.text,
    origin: claim.origin,
    personAuthored: claim.person_authored,
    date: claim.last_confirmed,
    expired: claim.status === "expired",
  };
}

/** `memory.*`: the Muse's Memory Profile and the person's editable claims, from the Muse session. */
export const memoryRouter = {
  memory: {
    profile: os.memory.profile.handler(async () => {
      const result = await onSuperChat(
        getOmnigentMemoryProfile(engineClient(), NO_EMAIL, await museSessionId()),
      );
      return { profile: result.profile };
    }),
    claims: os.memory.claims.handler(async () => {
      const claims = await onSuperChat(
        listOmnigentMemoryClaims(engineClient(), NO_EMAIL, await museSessionId()),
      );
      return { claims: claims.map(mapClaim) };
    }),
    editClaim: os.memory.editClaim.handler(async ({ input }) =>
      mapClaim(
        await onSuperChat(
          patchOmnigentMemoryClaim(
            engineClient(),
            NO_EMAIL,
            await museSessionId(),
            input.claimId,
            input.text,
          ),
        ),
      ),
    ),
    forgetClaim: os.memory.forgetClaim.handler(async ({ input }) => {
      await onSuperChat(
        forgetOmnigentMemoryClaim(engineClient(), NO_EMAIL, await museSessionId(), input.claimId),
      );
      return { ok: true as const };
    }),
  },
};
