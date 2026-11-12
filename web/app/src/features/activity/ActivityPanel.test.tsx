// @vitest-environment jsdom

import type { ReactNode } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../test/i18n")).withI18nRoot(await orig<typeof import("react-dom/client")>()),
);

import { ACTIVITY_LOADING_GRACE_MS, ActivityPanel } from "./ActivityPanel";
import { focusActivity } from "./activityFocus";
import { activity, helper, step } from "./activityTestKit";

vi.mock("@lingui/react/macro", () => {
  const t = (parts: TemplateStringsArray, ...values: unknown[]) =>
    parts.reduce((acc, part, i) => `${acc}${part}${values[i] ?? ""}`, "");
  return {
    useLingui: () => ({ t, i18n: { locale: "en" } }),
    Trans: ({ children }: { children: ReactNode }) => children,
  };
});
vi.mock("@lingui/core/macro", () => ({
  t: (parts: TemplateStringsArray, ...values: unknown[]) =>
    parts.reduce((acc, part, i) => `${acc}${part}${values[i] ?? ""}`, ""),
}));
vi.mock("./ActivityRunDialog", () => ({ ActivityRunDialog: () => null }));

const mounted: Array<() => void> = [];
afterEach(() => {
  for (const unmount of mounted.splice(0)) unmount();
});

async function mount(activities: ReturnType<typeof activity>[]) {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push(() => act(() => root.unmount()));
  await act(async () => {
    root.render(
      <ActivityPanel
        botId="bot-1"
        wire={{
          get: async () => activities[0] as never,
          helperMessages: async () => ({}) as never,
        }}
        state={{ status: "ready", activities, hasMore: false }}
        loadEarlier={async () => undefined}
        loadingEarlier={false}
      />,
    );
  });
  return container;
}

const startedAt = new Date(Date.now() - 80_000).toISOString();

it("puts what is running above the days, live, with its current step and how long it has gone", async () => {
  const container = await mount([
    activity({ id: "turn:old", title: "Checked the budget" }),
    helper("h1", {
      title: "Researching Japan trip",
      status: "in_progress",
      finishedAt: null,
      startedAt,
      steps: [step("Searched the web", 1)],
    }),
  ]);
  const working = container.querySelector("[data-testid=activity-working]");
  expect(working?.textContent).toContain("Working now");
  expect(working?.textContent).toContain("Researching Japan trip");
  expect(working?.textContent).toContain("Searched the web");
  expect(working?.querySelector("[data-testid=activity-elapsed]")?.textContent).toMatch(
    /^1m \d+s$/,
  );
  expect(working?.textContent).not.toContain("Checked the budget");
  expect(container.textContent).toContain("Checked the budget");
});

it("nests a Helper's parts under it instead of listing them again, and keeps it working while a part is", async () => {
  const container = await mount([
    helper("lead", { title: "Plan the trip", status: "done", startedAt }),
    helper("part", {
      parentChatId: "lead",
      title: "Find flights",
      status: "in_progress",
      finishedAt: null,
      startedAt,
    }),
  ]);
  const rows = [...container.querySelectorAll("[data-testid=activity-row]")];
  expect(
    rows.map((row) => row.querySelector("[data-testid=activity-line-title]")?.textContent),
  ).toEqual(["Plan the trip", "Find flights"]);
  expect(container.querySelector("[data-testid=activity-working]")).not.toBeNull();
  expect(container.querySelectorAll("[data-testid=activity-row]")).toHaveLength(2);
});

it("lists settled work by day with no working section", async () => {
  const container = await mount([activity({ id: "turn:1" })]);
  expect(container.querySelector("[data-testid=activity-working]")).toBeNull();
  expect(container.querySelector("[data-testid=activity-row]")?.getAttribute("data-status")).toBe(
    "done",
  );
});

async function mountLoading() {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push(() => act(() => root.unmount()));
  await act(async () => {
    root.render(
      <ActivityPanel
        botId="bot-1"
        wire={{ get: async () => ({}) as never, helperMessages: async () => ({}) as never }}
        state={{ status: "loading" }}
        loadEarlier={async () => undefined}
        loadingEarlier={false}
      />,
    );
  });
  return container;
}

it("shows the skeleton while the feed loads, then a quiet loading line, never the empty line", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  try {
    const container = await mountLoading();
    expect(container.querySelector("[data-testid=panel-skeleton]")).not.toBeNull();
    await act(async () => vi.advanceTimersByTime(ACTIVITY_LOADING_GRACE_MS));
    expect(container.querySelector("[data-testid=panel-skeleton]")).toBeNull();
    expect(container.querySelector("[data-testid=activity-empty]")).toBeNull();
    expect(container.querySelector("[data-testid=activity-loading]")).not.toBeNull();
  } finally {
    vi.useRealTimers();
  }
});

it("skips the skeleton while the page is hidden, since the feed is paused", async () => {
  const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
  try {
    const container = await mountLoading();
    expect(container.querySelector("[data-testid=panel-skeleton]")).toBeNull();
    expect(container.querySelector("[data-testid=activity-loading]")).not.toBeNull();
  } finally {
    visibility.mockRestore();
  }
});

it("brings the Helper that Show details asked for into view, once", async () => {
  const scrolled: string[] = [];
  // jsdom has no layout, so no scrollIntoView of its own.
  HTMLElement.prototype.scrollIntoView ??= () => undefined;
  const scrollIntoView = vi
    .spyOn(HTMLElement.prototype, "scrollIntoView")
    .mockImplementation(function (this: HTMLElement) {
      scrolled.push(this.getAttribute("data-activity-id") ?? "");
    });
  try {
    focusActivity("sub_agent:h2");
    const container = await mount([
      helper("h1", { status: "done" }),
      helper("h2", { title: "Write the deck", status: "in_progress", startedAt, finishedAt: null }),
    ]);
    expect(scrolled).toEqual(["sub_agent:h2"]);
    const row = container.querySelector('[data-activity-id="sub_agent:h2"]');
    expect(row?.classList.contains("sc-message-flash")).toBe(true);
    // Settled: a panel mounted later does not scroll again.
    await mount([helper("h2", { status: "in_progress", startedAt, finishedAt: null })]);
    expect(scrolled).toHaveLength(1);
  } finally {
    scrollIntoView.mockRestore();
  }
});

it("lists a file's studio turns under its name, inside the day, never as Helper rows", async () => {
  const deck = { groupId: "g-deck", name: "q3_review.deck.html" };
  const container = await mount([
    activity({
      id: "turn:studio:r2",
      kind: "sub_agent",
      source: "studio",
      chatId: "studio-1",
      parentChatId: "conv_muse",
      artifact: deck,
      title: "Biggest first",
    }),
    activity({ id: "turn:conv:r0", title: "Checked the budget" }),
    activity({
      id: "turn:studio:r1",
      source: "studio",
      chatId: "studio-1",
      artifact: deck,
      title: "Make it a bar chart",
    }),
  ]);
  const files = container.querySelectorAll("[data-testid=activity-file]");
  expect(files).toHaveLength(1);
  expect(files[0]?.textContent).toContain("q3_review.deck.html");
  const rows = [...container.querySelectorAll("[data-testid=activity-row]")].map(
    (row) => row.textContent ?? "",
  );
  expect(rows).toHaveLength(3);
  expect(rows[0]).toContain("Biggest first");
  expect(rows[1]).toContain("Make it a bar chart");
  expect(rows[2]).toContain("Checked the budget");
  // The file's turns follow its header, nested.
  expect(files[0]?.nextElementSibling?.getAttribute("data-activity-id")).toBe("turn:studio:r2");
  expect(files[0]?.nextElementSibling?.className).toContain("ps-6");
});
