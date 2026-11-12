import { describe, expect, it } from "vitest";
import { blockPlace, blockStyle, replaceBlock, restyleBlock, splitBlocks } from "./markdown-blocks";

const kinds = (source: string) => splitBlocks(source).map((b) => b.type);

describe("splitBlocks", () => {
  it("finds headings by level and text, ATX and setext", () => {
    const blocks = splitBlocks("# Brief #\n\nIntro\n\nGoals\n-----\n\n### Revenue\n");
    expect(blocks.map((b) => [b.type, b.level, b.text])).toEqual([
      ["heading", 1, "Brief"],
      ["paragraph", undefined, undefined],
      ["heading", 2, "Goals"],
      ["heading", 3, "Revenue"],
    ]);
  });

  it("keeps a fence whole, blank lines and markers inside it included", () => {
    const source = "Text\n\n```md\n# not a heading\n\n- not a list\n```\nAfter";
    const blocks = splitBlocks(source);
    expect(blocks.map((b) => b.type)).toEqual(["paragraph", "code", "paragraph"]);
    expect(blocks[1]?.source).toBe("```md\n# not a heading\n\n- not a list\n```");
    expect(kinds("~~~\nopen to the end\n\n# still code")).toEqual(["code"]);
  });

  it("keeps a list together across loose items and indented children", () => {
    const source = "- one\n  wrapped\n\n- two\n  - child\n1. three\n\nAfter the list";
    const blocks = splitBlocks(source);
    expect(blocks.map((b) => b.type)).toEqual(["list", "paragraph"]);
    expect(blocks[0]?.source).toBe("- one\n  wrapped\n\n- two\n  - child\n1. three");
  });

  it("tells tables, quotes and rules from paragraphs", () => {
    const source =
      "| a | b |\n|---|:-:|\n| 1 | 2 |\n\n> quoted\nlazy\n\n---\n\na | b without a rule";
    expect(kinds(source)).toEqual(["table", "quote", "rule", "paragraph"]);
  });

  it("gives every block its exact source span and lines", () => {
    const source = "# A\n\nfirst line\nsecond line\n\nlast";
    for (const block of splitBlocks(source)) {
      expect(source.slice(block.start, block.end)).toBe(block.source);
    }
    expect(splitBlocks(source)[1]).toMatchObject({ line: 3, lastLine: 4 });
  });

  it("starts a block right under a paragraph line when markdown would", () => {
    expect(kinds("Para\n# Heading\n- item")).toEqual(["paragraph", "heading", "list"]);
  });
});

describe("replaceBlock and blockPlace", () => {
  const source = "Lead\n\n## Goals\n\nGrow.\n\nHire later.\n\n- a\n";
  const blocks = splitBlocks(source);

  it("replaces one block and leaves every other byte alone", () => {
    const next = replaceBlock(source, blocks[3] as never, "Hire in October.");
    expect(next).toBe("Lead\n\n## Goals\n\nGrow.\n\nHire in October.\n\n- a\n");
  });

  it("numbers a block among its kind in its section", () => {
    expect(blockPlace(blocks, 0)).toEqual({ section: null, n: 1 });
    expect(blockPlace(blocks, 1)).toEqual({ section: "Goals", n: 1 });
    expect(blockPlace(blocks, 3)).toEqual({ section: "Goals", n: 2 });
    expect(blockPlace(blocks, 4)).toEqual({ section: "Goals", n: 1 });
  });
});

describe("restyleBlock", () => {
  const [para, list, heading, code] = splitBlocks(
    "Grow.\nHire later.\n\n- a\n- b\n\n## Goals\n\n```\nx\n```",
  );

  it("swaps markers and keeps the words", () => {
    expect(restyleBlock(para as never, "list")).toBe("- Grow.\n- Hire later.");
    expect(restyleBlock(list as never, "quote")).toBe("> a\n> b");
    expect(restyleBlock(list as never, "h2")).toBe("## a b");
    expect(restyleBlock(heading as never, "paragraph")).toBe("Goals");
  });

  it("only offers it for text blocks", () => {
    expect([para, list, heading, code].map((b) => blockStyle(b as never))).toEqual([
      "paragraph",
      "list",
      "h2",
      null,
    ]);
  });
});
