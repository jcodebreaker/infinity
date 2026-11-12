import { ORPCError } from "@orpc/client";

/** An engine API failure as the adapters log it (`omnigent <call> failed (<status>): <body>`),
 * which only ever belongs in a log. */
const RAW_ENGINE_ERROR = /\bfailed \(\d{3}\)|"error"\s*:\s*\{|^\s*\{.*"code"/s;

export function isRawEngineError(text: string): boolean {
  return RAW_ENGINE_ERROR.test(text);
}

/**
 * The words a person may read for a failed call: the message of an error the API chose to
 * return (an `ORPCError` other than the opaque internal one), else `fallback`. Network and
 * server failures carry technical text ("Internal server error", "Failed to fetch").
 */
export function userFacingError(error: unknown, fallback: string): string {
  if (
    error instanceof ORPCError &&
    error.code !== "INTERNAL_SERVER_ERROR" &&
    error.message &&
    !isRawEngineError(error.message)
  ) {
    return error.message;
  }
  return fallback;
}
