import type { Ask } from "@superchat/contracts";
import { describe, expect, it } from "vitest";
import { askDecision } from "./askDecision";

function ask(overrides: Partial<Ask>): Ask {
  return {
    id: "m1",
    runId: "r1",
    kind: "question",
    goalId: null,
    goalTitle: null,
    text: "Which evenings work?",
    choices: [],
    input: null,
    createdAt: "2026-10-07T10:00:00.000Z",
    ...overrides,
  };
}

const APPROVAL = "One yes before I send this";

describe("askDecision", () => {
  it("puts the first choice on the primary (right) button and the second on the secondary", () => {
    const decision = askDecision(
      ask({
        choices: [
          { id: "send", label: "Send" },
          { id: "not-now", label: "Not now" },
        ],
      }),
      APPROVAL,
    );
    expect(decision.actions?.primary).toEqual({ id: "send", label: "Send" });
    expect(decision.actions?.secondary).toEqual({ id: "not-now", label: "Not now" });
    expect(decision.tone).toBe("blue");
  });

  it("keeps a lone choice as the primary only", () => {
    const decision = askDecision(ask({ choices: [{ id: "ok", label: "OK" }] }), APPROVAL);
    expect(decision.actions).toEqual({ primary: { id: "ok", label: "OK" }, secondary: null });
  });

  it("opens Waiting on you for typed answers or more than two choices", () => {
    expect(askDecision(ask({ input: "text", choices: [] }), APPROVAL).actions).toBeNull();
    expect(
      askDecision(
        ask({
          choices: [
            { id: "a", label: "A" },
            { id: "b", label: "B" },
            { id: "c", label: "C" },
          ],
        }),
        APPROVAL,
      ).actions,
    ).toBeNull();
  });

  it("titles a plain approval in Superchat's voice, with what it would send underneath", () => {
    const decision = askDecision(
      ask({ kind: "approval", text: "Send the comparison to Dana?", detail: "To: Dana" }),
      APPROVAL,
    );
    expect(decision.title).toBe(APPROVAL);
    expect(decision.subtitle).toBe("Send the comparison to Dana?");
    expect(decision.tone).toBe("orange");
  });

  it("titles an engine-held approval with its own text and detail", () => {
    const decision = askDecision(
      ask({
        kind: "approval",
        text: "Run the deploy script?",
        detail: "It touches production.",
        approval: {},
      }),
      APPROVAL,
    );
    expect(decision.title).toBe("Run the deploy script?");
    expect(decision.subtitle).toBe("It touches production.");
  });
});
