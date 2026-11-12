// @vitest-environment jsdom
import { expect, it } from "vitest";
import { markParts } from "./mark-parts";

it("adds one rule for the ids at the top of the head", () => {
  const out = markParts(
    "<html><head><title>x</title></head><body></body></html>",
    ["a", "b"],
    "red",
  );
  expect(out).toContain(
    '<head><style data-superchat-marked>[data-superchat-id="a"],[data-superchat-id="b"]{',
  );
  expect(out).toContain("color-mix(in srgb,red 14%,transparent)");
});

it("leaves the page alone with nothing to mark, and cannot be closed early by an id", () => {
  expect(markParts("<p>x</p>", [], "red")).toBe("<p>x</p>");
  expect(markParts("<p>x</p>", ["</style><script>"], "red")).not.toContain("</style><script>");
});
