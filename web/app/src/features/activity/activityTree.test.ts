import { expect, it } from "vitest";
import { activity, helper, step } from "./activityTestKit";
import {
  buildActivityForest,
  findHelperNode,
  isNodeRunning,
  latestStepTitle,
  stepCount,
} from "./activityTree";

const lead = helper("lead", { status: "in_progress", finishedAt: null });
const partA = helper("a", {
  parentChatId: "lead",
  startedAt: "2026-10-03T11:25:00.000Z",
  status: "done",
});
const partB = helper("b", {
  parentChatId: "lead",
  startedAt: "2026-10-03T11:23:00.000Z",
  status: "in_progress",
  finishedAt: null,
});

it("nests a Helper's parts under it, oldest part first, and lists nothing twice", () => {
  const turn = activity({ id: "turn:1" });
  const forest = buildActivityForest([partA, turn, lead, partB]);
  expect(forest.map((node) => node.activity.id)).toEqual(["turn:1", "sub_agent:lead"]);
  expect(forest[1]?.children.map((node) => node.activity.chatId)).toEqual(["b", "a"]);
});

it("keeps a Helper with no listed coordinator or Round at the top, else under its Round's row", () => {
  const orphan = helper("orphan", { parentChatId: "not-loaded" });
  expect(buildActivityForest([orphan, lead]).map((node) => node.activity.chatId)).toEqual([
    "orphan",
    "lead",
  ]);
  const round = activity({ id: "round:m1", roundId: "m1" });
  const started = helper("lead", { roundId: "m1", status: "in_progress", finishedAt: null });
  const unlisted = helper("other", { roundId: "m2" });
  const forest = buildActivityForest([started, round, partA, unlisted]);
  expect(forest.map((node) => node.activity.id)).toEqual(["round:m1", "sub_agent:other"]);
  expect(forest[0]?.children.map((node) => node.activity.chatId)).toEqual(["lead"]);
  expect(forest[0]?.children[0]?.children.map((node) => node.activity.chatId)).toEqual(["a"]);
  expect(forest[0] && isNodeRunning(forest[0])).toBe(true);
});

it("reads a node as running while it or any part under it is", () => {
  const settledLead = helper("lead", { status: "done" });
  const [node] = buildActivityForest([settledLead, partB]);
  expect(node && isNodeRunning(node)).toBe(true);
  const [calm] = buildActivityForest([settledLead, partA]);
  expect(calm && isNodeRunning(calm)).toBe(false);
});

it("finds a Helper's node by its chat, at any depth", () => {
  const forest = buildActivityForest([lead, partA, partB]);
  expect(findHelperNode(forest, "b")?.activity.id).toBe("sub_agent:b");
  expect(findHelperNode(forest, "nope")).toBeUndefined();
});

it("counts steps and names the latest one in plain words", () => {
  const working = helper("w", { steps: [step("Searched the web", 1), step("Read a page", 2)] });
  expect(stepCount(working)).toBe(2);
  expect(latestStepTitle(working)).toBe("Read a page");
  expect(stepCount(helper("w"))).toBe(0);
  expect(latestStepTitle(helper("w"))).toBeUndefined();
});

it("builds one forest per feed snapshot, shared by every reader", () => {
  const feed = [lead, partA, partB];
  const forest = buildActivityForest(feed);
  expect(buildActivityForest(feed)).toBe(forest);
  expect(buildActivityForest([...feed])).not.toBe(forest);
  expect(findHelperNode(forest, "a")?.activity.id).toBe("sub_agent:a");
  expect(findHelperNode(forest, "lead")).toBe(forest[0]);
});
