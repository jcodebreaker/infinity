// @vitest-environment jsdom

import type { ReactNode } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../test/i18n")).withI18nRoot(await orig<typeof import("react-dom/client")>()),
);

import { type DailyNote, DaysSection } from "./DaysSection";

vi.mock("@lingui/react/macro", () => {
  const t = (parts: TemplateStringsArray, ...values: unknown[]) =>
    parts.reduce((acc, part, i) => `${acc}${part}${values[i] ?? ""}`, "");
  return {
    useLingui: () => ({ t, i18n: { locale: "en" } }),
    Trans: ({ children }: { children: ReactNode }) => children,
  };
});

const note: DailyNote = {
  date: "2026-10-04",
  sections: { talked_about: "- budget", decisions: "", promised: "- send deck", open_loops: "" },
  editedByPerson: false,
  finalized: false,
  updatedAt: 1,
};

const mounted: Array<() => void> = [];
afterEach(() => {
  for (const unmount of mounted.splice(0)) unmount();
});

async function mount(wire: Parameters<typeof DaysSection>[0]["wire"]) {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push(() => act(() => root.unmount()));
  await act(async () => {
    root.render(<DaysSection botId="b1" wire={wire} />);
  });
  return container;
}

const button = (container: HTMLElement, label: string) =>
  [...container.querySelectorAll("button")].find((b) => b.textContent === label) as HTMLElement;

it("lists a day and saves an inline edit through the wire", async () => {
  const save = vi.fn(async (input: { sections: Record<string, string> }) => ({
    ...note,
    sections: input.sections,
    editedByPerson: true,
  }));
  const container = await mount({ list: async () => ({ notes: [note] }), save });
  expect(container.textContent).toContain("- send deck");

  await act(async () => button(container, "Edit").click());
  const boxes = container.querySelectorAll("textarea");
  expect(boxes).toHaveLength(5);
  const box = boxes[2] as HTMLTextAreaElement;
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    setter?.call(box, "- send deck monday");
    box.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => button(container, "Save").click());

  expect(save).toHaveBeenCalledWith(expect.objectContaining({ botId: "b1", date: "2026-10-04" }));
  expect(container.textContent).toContain("- send deck monday");
  expect(container.querySelector("textarea")).toBeNull();
});

it("renders nothing when the load fails", async () => {
  const container = await mount({
    list: async () => {
      throw new Error("down");
    },
    save: vi.fn(),
  });
  expect(container.textContent).toBe("");
});
