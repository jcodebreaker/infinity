import { describe, expect, it } from "vitest";
import { ArtifactVersionSchema, chatsContract, ThreadMessageSchema } from "./index.js";

// docs/conversation.md "Contract": a message links to an artifact, a version links to a message.

const message = {
  id: "item_1",
  threadId: "conv_1",
  seq: 0,
  role: "user",
  blocks: [{ kind: "text", text: "Make the title bigger" }],
  createdAt: "2026-10-17T10:00:00.000Z",
};

describe("conversation contract", () => {
  it("a message may carry the artifact it is about, or none", () => {
    const linked = {
      ...message,
      artifact: { artifactId: "art_1", name: "Q3 Review.html", version: 4, how: "edited" },
    };
    expect(ThreadMessageSchema.parse(linked).artifact).toEqual(linked.artifact);
    expect(ThreadMessageSchema.parse({ ...message, artifact: null }).artifact).toBeNull();
    expect(ThreadMessageSchema.parse(message).artifact).toBeUndefined();
    expect(
      ThreadMessageSchema.parse({
        ...message,
        artifact: { artifactId: "art_1", name: "a.html", version: null, how: "open" },
      }).artifact?.version,
    ).toBeNull();
    expect(
      ThreadMessageSchema.safeParse({
        ...message,
        artifact: { artifactId: "art_1", name: "a.html", version: 1, how: "guessed" },
      }).success,
    ).toBe(false);
  });

  it("send takes the artifact open beside the chat; relink answers with the message", () => {
    const send = chatsContract.chats.send["~orpc"].inputSchema;
    expect(
      send?.safeParse({
        botId: "conv_1",
        text: "hi",
        context: { openArtifact: { artifactId: "art_1", version: 2 } },
      }).success,
    ).toBe(true);
    expect(
      send?.safeParse({
        botId: "conv_1",
        text: "hi",
        context: { openArtifact: { artifactId: "art_1", version: 0 } },
      }).success,
    ).toBe(false);
    const relink = chatsContract.chats.setMessageArtifact["~orpc"].inputSchema;
    expect(relink?.safeParse({ botId: "b", itemId: "i", artifactId: null }).success).toBe(true);
    expect(relink?.safeParse({ botId: "b", itemId: "i", artifactId: "art_1" }).success).toBe(true);
    expect(relink?.safeParse({ botId: "b", itemId: "i" }).success).toBe(false);
  });

  it("a version may name the message that led to it", () => {
    const version = { id: "art_1", version: 2, name: "a.html", createdAt: "x" };
    expect(ArtifactVersionSchema.parse({ ...version, itemId: "item_1" }).itemId).toBe("item_1");
    expect(ArtifactVersionSchema.parse({ ...version, itemId: null }).itemId).toBeNull();
    expect(ArtifactVersionSchema.parse(version).itemId).toBeUndefined();
  });
});
