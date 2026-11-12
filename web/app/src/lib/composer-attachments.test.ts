import { expect, it } from "vitest";
import {
  clearComposerAttachments,
  registerAttachmentParser,
  setComposerAttachment,
  splitComposerAttachments,
  withComposerAttachments,
} from "./composer-attachments";

// A capability registers its reader when it loads; two stand-ins with distinct fenced blocks.
const fence = (name: string) => (text: string) => {
  const open = `<${name}>`;
  const close = `\n</${name}>`;
  if (!text.startsWith(open) || !text.includes(close)) return null;
  const after = text.slice(text.indexOf(close) + close.length);
  return { label: `${name} chip`, rest: after.replace(/^\n\n/, "") };
};
registerAttachmentParser("a", fence("a"));
registerAttachmentParser("b", fence("b"));

it("keeps one attachment per kind and several kinds side by side", () => {
  clearComposerAttachments();
  setComposerAttachment("a", { label: "x", block: "<a>\n1\n</a>" });
  setComposerAttachment("a", { label: "y", block: "<a>\n2\n</a>" });
  const sent = withComposerAttachments("why?", [
    { kind: "a", label: "y", block: "<a>\n2\n</a>" },
    { kind: "b", label: "z", block: "<b>\n3\n</b>" },
  ]);
  expect(sent).toBe("<a>\n2\n</a>\n\n<b>\n3\n</b>\n\nwhy?");
  clearComposerAttachments();
});

it("reads every leading block back into a chip, in order", () => {
  expect(splitComposerAttachments("<a>\n2\n</a>\n\n<b>\n3\n</b>\n\nwhy?")).toEqual({
    chips: [
      { kind: "a", label: "a chip" },
      { kind: "b", label: "b chip" },
    ],
    rest: "why?",
  });
  expect(splitComposerAttachments("hi <a>\n2\n</a>")).toEqual({
    chips: [],
    rest: "hi <a>\n2\n</a>",
  });
});
