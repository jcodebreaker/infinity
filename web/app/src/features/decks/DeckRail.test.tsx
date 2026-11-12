// @vitest-environment jsdom

import { i18n } from "@lingui/core";
import { I18nProvider } from "@lingui/react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

vi.mock("@lingui/react/macro", () => ({
  useLingui: () => ({
    t: (parts: TemplateStringsArray, ...v: unknown[]) =>
      parts.reduce((out, part, i) => out + part + (v[i] ?? ""), ""),
  }),
}));

import { resetNavigatorFrame } from "../artifacts/editor/NavigatorFrame";
import { DeckRail } from "./DeckRail";
import { FIXTURE_DECK } from "./deck-fixture";
import { parseDeckSlides } from "./deck-model";

i18n.loadAndActivate({ locale: "en", messages: {} });

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});
afterEach(() => {
  resetNavigatorFrame();
  document.body.innerHTML = "";
});

async function mount(onSelect = vi.fn()) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () =>
    root.render(
      <I18nProvider i18n={i18n}>
        <DeckRail
          html={FIXTURE_DECK}
          slides={parseDeckSlides(FIXTURE_DECK)}
          active={0}
          onSelect={onSelect}
        />
      </I18nProvider>,
    ),
  );
  return container;
}
const rail = (c: HTMLElement) => c.querySelector('[data-testid="deck-rail"]') as HTMLElement;
const button = (c: HTMLElement, label: string) =>
  c.querySelector(`button[aria-label="${label}"]`) as HTMLButtonElement;

it("shows every slide as a numbered thumbnail, the current one selected", async () => {
  const onSelect = vi.fn();
  const c = await mount(onSelect);
  expect(rail(c).dataset.collapsed).toBeUndefined();
  const options = Array.from(c.querySelectorAll('[role="option"]'));
  expect(options).toHaveLength(3);
  expect(options.map((o) => o.querySelector("span.tabular-nums")?.textContent)).toEqual([
    "1",
    "2",
    "3",
  ]);
  expect(options[0]?.getAttribute("aria-selected")).toBe("true");
  await act(async () => (options[1] as HTMLButtonElement).click());
  expect(onSelect).toHaveBeenCalledWith(1);
});

it("folds to a strip of slide numbers and back, remembered for the session", async () => {
  const onSelect = vi.fn();
  const c = await mount(onSelect);
  await act(async () => button(c, "Hide slides").click());
  expect(rail(c).dataset.collapsed).toBe("true");
  expect(button(c, "Show slides").getAttribute("aria-expanded")).toBe("false");
  const numbers = Array.from(c.querySelectorAll('[role="option"]'));
  expect(numbers.map((o) => o.textContent)).toEqual(["1", "2", "3"]);
  expect(numbers[0]?.getAttribute("aria-current")).toBe("true");
  await act(async () => (numbers[2] as HTMLButtonElement).click());
  expect(onSelect).toHaveBeenCalledWith(2);

  // Another rail (edit mode, another deck) opens folded too.
  const again = await mount();
  expect(rail(again).dataset.collapsed).toBe("true");
  await act(async () => button(again, "Show slides").click());
  expect(rail(again).dataset.collapsed).toBeUndefined();
});
