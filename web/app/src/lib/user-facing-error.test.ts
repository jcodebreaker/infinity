import { ORPCError } from "@orpc/client";
import { describe, expect, it } from "vitest";
import { isRawEngineError, userFacingError } from "./user-facing-error";

describe("userFacingError", () => {
  it("keeps the copy of an error the API chose to return", () => {
    const error = new ORPCError("CONFLICT", { message: "You already have a Conversation here." });
    expect(userFacingError(error, "Failed")).toBe("You already have a Conversation here.");
  });

  it("hides internal and network failures behind the fallback", () => {
    expect(userFacingError(new ORPCError("INTERNAL_SERVER_ERROR"), "Failed")).toBe("Failed");
    expect(userFacingError(new TypeError("Failed to fetch"), "Failed")).toBe("Failed");
  });

  it("never shows an engine status line or body", () => {
    const raw = 'omnigent get muse failed (400): {"error":{"code":"invalid_input"}}';
    expect(userFacingError(new ORPCError("BAD_REQUEST", { message: raw }), "Failed")).toBe(
      "Failed",
    );
    expect(isRawEngineError(raw)).toBe(true);
    expect(isRawEngineError("The model is busy.")).toBe(false);
  });
});
