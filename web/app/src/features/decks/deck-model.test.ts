// @vitest-environment jsdom

import { expect, it } from "vitest";
import { FIXTURE_DECK } from "./deck-fixture";
import { parseDeckSlides, readDeckEvent } from "./deck-model";

it("reads slide labels and titles without running the deck", () => {
  expect(parseDeckSlides(FIXTURE_DECK)).toEqual([
    { index: 0, label: "01 Title", title: "Title" },
    { index: 1, label: "02 Problem", title: "Problem" },
    { index: 2, label: "03 Plan", title: "Plan" },
  ]);
});

it("ignores presenter clones and returns nothing for a plain page", () => {
  const html = `<div class="overview"><section class="slide" data-screen-label="01 X"></section></div>`;
  expect(parseDeckSlides(html)).toEqual([]);
  expect(parseDeckSlides("<p>hi</p>")).toEqual([]);
});

it("reads only well-formed deck protocol messages", () => {
  expect(readDeckEvent({ type: "superchat:deck-ready", protocolVersion: 1 })).toEqual({
    kind: "ready",
  });
  expect(
    readDeckEvent({ type: "superchat:slide-state", protocolVersion: 1, active: 2, count: 3 }),
  ).toEqual({ kind: "state", active: 2, count: 3 });
  expect(
    readDeckEvent({ type: "superchat:slide-state", protocolVersion: 2, active: 2, count: 3 }),
  ).toBeNull();
  expect(
    readDeckEvent({ type: "superchat:slide-state", protocolVersion: 1, active: "2" }),
  ).toBeNull();
  expect(readDeckEvent("x")).toBeNull();
});
