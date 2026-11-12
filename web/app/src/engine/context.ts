import { implement, ORPCError } from "@orpc/server";
import {
  isSessionNotFoundError,
  OmnigentApiError,
  omnigentErrorCopy,
} from "@superchat/adapters/omnigent/browser";
import { appContract } from "@superchat/contracts";

/** The router builder every `engine/<capability>.ts` implements its procedures with. */
export const os = implement(appContract);

/** The RPC status an engine error code with client copy surfaces as. */
const STATUS: Record<string, "FORBIDDEN" | "SERVICE_UNAVAILABLE" | "CONFLICT" | "BAD_REQUEST"> = {
  helper_read_only: "FORBIDDEN",
  superchat_not_configured: "SERVICE_UNAVAILABLE",
  muse_already_set: "CONFLICT",
  muse_tenant_mismatch: "CONFLICT",
  not_a_super_chat: "BAD_REQUEST",
};

/** An engine call on a Super Chat the engine no longer has reads as "no Conversation yet"; the
 * next turn recreates it. An engine error code with client copy (`omnigentErrorCopy`) surfaces
 * as that copy. Every other failure passes through unchanged. */
export async function onSuperChat<T>(call: Promise<T>): Promise<T> {
  try {
    return await call;
  } catch (error) {
    if (isSessionNotFoundError(error)) {
      throw new ORPCError("NOT_FOUND", { message: "This Muse has no Conversation yet" });
    }
    const copy = omnigentErrorCopy(error);
    if (copy && error instanceof OmnigentApiError && error.code) {
      throw new ORPCError(STATUS[error.code] ?? "BAD_REQUEST", { message: copy });
    }
    throw error;
  }
}
