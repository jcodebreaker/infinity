// @vitest-environment jsdom

import type { ReactNode } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../test/i18n")).withI18nRoot(await orig<typeof import("react-dom/client")>()),
);

import { ActivityRunDialog } from "./ActivityRunDialog";
import { activity, helper, step } from "./activityTestKit";

vi.mock("@lingui/react/macro", () => {
  const t = (parts: TemplateStringsArray, ...values: unknown[]) =>
    parts.reduce((acc, part, i) => `${acc}${part}${values[i] ?? ""}`, "");
  return {
    useLingui: () => ({ t, i18n: { locale: "en" } }),
    Trans: ({ children }: { children: ReactNode }) => children,
  };
});
vi.mock("./MessageRow", () => ({ MessageRow: () => null }));

const mounted: Array<() => void> = [];
afterEach(() => {
  for (const unmount of mounted.splice(0)) unmount();
  document.body.innerHTML = "";
});

async function mount(run: ReturnType<typeof activity>) {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push(() => act(() => root.unmount()));
  await act(async () => {
    root.render(
      <ActivityRunDialog
        botId="bot-1"
        wire={{ get: async () => run, helperMessages: async () => ({}) as never }}
        activity={run}
        onOpenChange={() => undefined}
      />,
    );
  });
}

it("shows a scheduled run that never started as 'Couldn't run' with its reason, not Done", async () => {
  await mount(
    activity({
      kind: "sub_agent",
      source: "scheduled",
      title: "8am market and tech report",
      status: "failed",
      outcome: "Your Computer didn't start.",
      summary: "Your Computer didn't start.",
      steps: [],
    }),
  );
  const text = document.body.textContent ?? "";
  expect(text).toContain("Couldn't run");
  expect(text).toContain("Your Computer didn't start.");
  expect(text).not.toContain("Done");
});

it("still says why when the failed run has no outcome text", async () => {
  await mount(activity({ status: "failed", outcome: null, summary: null, steps: [] }));
  const text = document.body.textContent ?? "";
  expect(text).toContain("Couldn't run");
  expect(text).toContain("Something went wrong before it could start.");
});

function toolStep(n: number, tool: string, title: string) {
  return { ...step(title, n), tool };
}

const STORY = [
  toolStep(1, "sys_os_shell", "Ran a command"),
  toolStep(2, "report_progress", "Read the Q2 review for its structure"),
  toolStep(3, "sys_os_read", "Read a file"),
  toolStep(4, "sys_os_shell", "Ran a command"),
  toolStep(5, "raise_flag", "Flagged: the memo and the ledger disagree"),
  toolStep(6, "sys_os_write", "Wrote a file"),
  toolStep(7, "report_progress", "Slide 6 of 10 written"),
  toolStep(8, "sys_os_shell", "Ran a command"),
  toolStep(9, "sys_os_shell", "Ran a command"),
  toolStep(10, "sys_os_edit", "Edited a file"),
  toolStep(11, "artifact_save", "Saved the deck"),
  toolStep(12, "web_search", "Searched"),
];

const topLevel = () =>
  [...document.querySelectorAll('[data-testid="activity-steps"] > li')].map((li) => li);
const headingTitle = (li: Element) =>
  li.querySelector("summary span span:nth-child(2)")?.textContent;

it("groups a Helper's steps under its progress headings, folded by default", async () => {
  await mount(helper("h-1", { status: "done", steps: STORY }));
  const rows = topLevel();
  expect(rows.map((li) => li.getAttribute("data-testid") ?? li.textContent)).toEqual([
    "story-heading",
    "story-heading",
    expect.stringContaining("Flagged: the memo and the ledger disagree"),
    "story-heading",
    expect.stringContaining("Saved a file"),
  ]);
  expect(headingTitle(rows[0] as Element)).toBe("Getting started");
  expect(headingTitle(rows[1] as Element)).toBe("Read the Q2 review for its structure");
  expect(headingTitle(rows[3] as Element)).toBe("Slide 6 of 10 written");
  expect(rows[0]?.textContent).toContain("1 command");
  expect(rows[1]?.textContent).toContain("1 command · 1 file · looked at 1 file");
  expect(rows[3]?.textContent).toContain("2 commands · 1 file · 1 step");
  for (const details of document.querySelectorAll('[data-testid="story-heading"] details')) {
    expect(details.hasAttribute("open")).toBe(false);
  }
  expect(rows[3]?.querySelectorAll("details ol > li")).toHaveLength(4);
});

it("marks only the last heading live while the Helper runs", async () => {
  await mount(helper("h-1", { status: "in_progress", steps: STORY }));
  const pulsing = (li: Element | undefined) =>
    li?.querySelector(":scope > span svg")?.getAttribute("class")?.includes("animate-");
  const headings = document.querySelectorAll('[data-testid="story-heading"]');
  expect([...headings].map((li) => pulsing(li))).toEqual([false, false, true]);
});

it("settles every heading once the Helper is done", async () => {
  await mount(helper("h-1", { status: "done", steps: STORY }));
  const icons = [...document.querySelectorAll('[data-testid="story-heading"] > span svg')];
  expect(icons.map((svg) => svg.getAttribute("class"))).toEqual([
    expect.stringContaining("lucide-check"),
    expect.stringContaining("lucide-check"),
    expect.stringContaining("lucide-check"),
  ]);
});

it("keeps the flat list for a Helper that never reported progress", async () => {
  await mount(
    helper("h-1", {
      steps: [toolStep(1, "sys_os_shell", "Ran"), toolStep(2, "raise_flag", "Flagged: x")],
    }),
  );
  expect(document.querySelector('[data-testid="story-heading"]')).toBeNull();
  expect(topLevel()).toHaveLength(2);
});
