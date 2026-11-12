import type { ThreadMessage } from "@superchat/contracts";
import { describe, expect, it } from "vitest";
import { foldFailureRuns, isFailureNote, latestModelFailure } from "./failureNotes";

let seq = 0;
function message(role: ThreadMessage["role"], blocks: ThreadMessage["blocks"]): ThreadMessage {
  seq += 1;
  return {
    id: `m${seq}`,
    threadId: "t1",
    seq,
    role,
    blocks,
    createdAt: new Date(Date.UTC(2026, 9, 7, 12, seq)).toISOString(),
  };
}
const failure = (code = "sandbox_unavailable") => message("bot", [{ kind: "error", code }]);
const reply = (text: string) => message("bot", [{ kind: "text", text }]);
const ask = (text: string) => message("user", [{ kind: "text", text }]);

const shape = (rows: ReturnType<typeof foldFailureRuns>) =>
  rows.map((row) => (row.kind === "message" ? row.message.id : row.messages.map((m) => m.id)));

describe("isFailureNote", () => {
  it("is a Muse reply made only of error blocks", () => {
    expect(isFailureNote(failure())).toBe(true);
    expect(isFailureNote(reply("Done."))).toBe(false);
    expect(isFailureNote(message("bot", []))).toBe(false);
    expect(
      isFailureNote(
        message("bot", [
          { kind: "text", text: "Partly" },
          { kind: "error", code: "x" },
        ]),
      ),
    ).toBe(false);
  });

  it("is never an info notice: something that happened is not a failed reply", () => {
    const notice = message("bot", [{ kind: "error", code: "workspace_reset", level: "info" }]);
    expect(isFailureNote(notice)).toBe(false);
    expect(shape(foldFailureRuns([failure(), notice, failure()]))).toEqual([
      expect.any(String),
      notice.id,
      expect.any(String),
    ]);
  });
});

describe("foldFailureRuns", () => {
  it("folds failures in a row into one row, in order", () => {
    const first = ask("Plan my week");
    const a = failure();
    const b = failure();
    const c = failure("timeout");
    expect(shape(foldFailureRuns([first, a, b, c]))).toEqual([first.id, [a.id, b.id, c.id]]);
  });

  it("leaves a lone failure as an ordinary message", () => {
    const a = failure();
    const next = ask("Try again");
    expect(foldFailureRuns([a, next])).toEqual([
      { kind: "message", message: a },
      { kind: "message", message: next },
    ]);
  });

  it("starts a new run after anything else, so separate runs stay separate", () => {
    const a = failure();
    const b = failure();
    const retry = ask("Again");
    const c = failure();
    const d = failure();
    const ok = reply("Here it is.");
    expect(shape(foldFailureRuns([a, b, retry, c, d, ok]))).toEqual([
      [a.id, b.id],
      retry.id,
      [c.id, d.id],
      ok.id,
    ]);
  });

  it("returns nothing for nothing", () => {
    expect(foldFailureRuns([])).toEqual([]);
  });
});

describe("latestModelFailure", () => {
  it("names the latest reply when it failed for a model-layer reason", () => {
    const failed = failure("model_key_required");
    expect(latestModelFailure([ask("hi"), failed])).toEqual({
      id: failed.id,
      code: "model_key_required",
    });
  });

  it("is gone once anything follows the failure, and for other failures", () => {
    expect(latestModelFailure([failure("model_key_required"), ask("retry")])).toBeNull();
    expect(latestModelFailure([ask("hi"), failure("sandbox_unavailable")])).toBeNull();
    expect(latestModelFailure([ask("hi"), reply("Done.")])).toBeNull();
    expect(latestModelFailure([])).toBeNull();
  });
});
