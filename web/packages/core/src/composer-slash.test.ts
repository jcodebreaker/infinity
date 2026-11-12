import { describe, expect, it } from "vitest";

import { SLASH_ACTIONS } from "./composer-slash.js";

describe("SLASH_ACTIONS", () => {
  it("exposes chat and account destinations", () => {
    expect(SLASH_ACTIONS.map((action) => action.id)).toEqual(["chat-settings", "settings-general"]);
  });
});
