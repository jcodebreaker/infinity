import { describe, expect, it } from "vitest";
import { markdownToLine, markdownToPlainText } from "./markdown-plain-text";

describe("markdownToPlainText", () => {
  it("splits the opening heading from the body and strips syntax", () => {
    expect(
      markdownToPlainText(
        "## AI Agent Launches\n\n**Major launches:**\n- [One](https://a.com) shipped",
      ),
    ).toEqual({
      heading: "AI Agent Launches",
      text: "Major launches: One shipped",
    });
  });
});

describe("markdownToLine", () => {
  it("joins heading and body into one plain line", () => {
    expect(markdownToLine("# AI agent launches\n**Major launches:**")).toBe(
      "AI agent launches Major launches:",
    );
  });
});
