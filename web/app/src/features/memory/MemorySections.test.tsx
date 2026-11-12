// @vitest-environment jsdom

import type { ReactNode } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../test/i18n")).withI18nRoot(await orig<typeof import("react-dom/client")>()),
);

import { type ClaimsWire, groupClaims, type MemoryClaim } from "./MemorySections";
import { MemoryTab } from "./MemoryTab";

vi.mock("@lingui/react/macro", () => {
  const t = (parts: TemplateStringsArray, ...values: unknown[]) =>
    parts.reduce((acc, part, i) => `${acc}${part}${values[i] ?? ""}`, "");
  return {
    useLingui: () => ({ t, i18n: { locale: "en" } }),
    Trans: ({ children }: { children: ReactNode }) => children,
  };
});

vi.mock("../../components/chrome/PanelSkeleton", () => ({ PanelRowSkeletonList: () => null }));

const claim = (over: Partial<MemoryClaim>): MemoryClaim => ({
  id: "c1",
  kind: "fact",
  text: "The user works in finance.",
  origin: "said",
  personAuthored: false,
  date: 1_791_000_000,
  ...over,
});

const mounted: Array<() => void> = [];
afterEach(() => {
  for (const unmount of mounted.splice(0)) unmount();
});

it("maps claim kinds to sections", () => {
  const groups = groupClaims([
    claim({ id: "a", kind: "preference" }),
    claim({ id: "b", kind: "commitment" }),
    claim({ id: "c", kind: "decision" }),
    claim({ id: "d", kind: "person" }),
    claim({ id: "e", kind: "working_style" }),
  ]);
  expect(groups.map((g) => g.id)).toEqual([
    "about",
    "commitments",
    "projects",
    "people",
    "working",
  ]);
});

it("puts open questions in their own group after Projects & focus", () => {
  const groups = groupClaims([
    claim({ id: "a", kind: "project", text: "The user is focused on the deck." }),
    claim({ id: "b", kind: "project", text: "The person needs to resolve whether A or B." }),
    claim({ id: "c", kind: "decision", text: "Should we pilot first?" }),
  ]);
  expect(groups.map((g) => [g.id, g.claims.map((c) => c.id)])).toEqual([
    ["projects", ["a"]],
    ["questions", ["b", "c"]],
  ]);
});

const flush = () => act(async () => {});

it("shows short labels, edits inline with Enter, and forgets with an undo window", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.useFakeTimers();
  const editClaim = vi.fn(async (input: { text: string }) =>
    claim({ text: input.text, origin: "edited", personAuthored: true }),
  );
  const forgetClaim = vi.fn(async () => ({ ok: true as const }));
  const wire: ClaimsWire & { profile: () => Promise<{ profile: null }> } = {
    profile: async () => ({ profile: null }),
    claims: async () => ({
      claims: [
        claim({}),
        claim({ id: "p", kind: "person", text: "Maya Chen is your manager; runs Platform." }),
        claim({
          id: "t",
          kind: "commitment",
          text: "You will send the deck by Oct 8.",
        }),
      ],
    }),
    editClaim,
    forgetClaim,
  };
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push(() => act(() => root.unmount()));
  await act(async () => {
    root.render(<MemoryTab botId="b1" wire={wire} />);
  });
  const text = () => container.textContent ?? "";
  expect(text()).toContain("About you");
  expect(text()).toContain("People");
  expect(text()).toContain("Works in finance");
  expect(text()).not.toContain("The user works");
  expect(text()).not.toContain("You said");
  expect(text()).toContain("Maya Chen");
  expect(text()).toContain("manager");
  expect(text()).toContain("Send the deck");

  // Edit from the "…" menu, save with Enter.
  await act(async () =>
    (container.querySelector('button[aria-label="More"]') as HTMLElement).click(),
  );
  const edit = [...document.body.querySelectorAll('[role="menuitem"]')].find(
    (item) => item.textContent === "Edit",
  ) as HTMLElement;
  await act(async () => edit.click());
  const box = container.querySelector("textarea") as HTMLTextAreaElement;
  expect(box.value).toBe("The user works in finance.");
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    setter?.call(box, "You work in banking.");
    box.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => {
    box.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  });
  expect(editClaim).toHaveBeenCalledWith({
    botId: "b1",
    claimId: "c1",
    text: "You work in banking.",
  });
  expect(text()).toContain("Works in banking");

  // Ticking a commitment can be undone; otherwise it forgets after the window.
  const tick = container.querySelector('[role="checkbox"]') as HTMLElement;
  await act(async () => tick.click());
  await act(async () => {
    (
      [...container.querySelectorAll("button")].find((b) => b.textContent === "Undo") as HTMLElement
    ).click();
  });
  await act(async () => {
    vi.advanceTimersByTime(5000);
  });
  expect(forgetClaim).not.toHaveBeenCalled();
  await act(async () => (container.querySelector('[role="checkbox"]') as HTMLElement).click());
  await act(async () => {
    vi.advanceTimersByTime(5000);
  });
  await flush();
  expect(forgetClaim).toHaveBeenCalledWith({ botId: "b1", claimId: "t" });
  expect(text()).not.toContain("Send the deck");
  vi.useRealTimers();
});

it("filters across sections by the search text", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const wire: ClaimsWire & { profile: () => Promise<{ profile: null }> } = {
    profile: async () => ({ profile: null }),
    claims: async () => ({
      claims: [claim({}), claim({ id: "p", kind: "person", text: "Maya Chen is your manager." })],
    }),
    editClaim: async () => claim({}),
    forgetClaim: async () => ({ ok: true as const }),
  };
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push(() => act(() => root.unmount()));
  await act(async () => {
    root.render(<MemoryTab botId="b1" wire={wire} />);
  });
  const input = container.querySelector('input[type="search"]') as HTMLInputElement;
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter?.call(input, "maya");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  expect(container.textContent).toContain("Maya Chen");
  expect(container.textContent).not.toContain("Works in finance");
});

it("marks an expired memory and offers only Forget for it", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const wire: ClaimsWire & { profile: () => Promise<{ profile: null }> } = {
    profile: async () => ({ profile: null }),
    claims: async () => ({ claims: [claim({ expired: true })] }),
    editClaim: async () => claim({}),
    forgetClaim: async () => ({ ok: true as const }),
  };
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  mounted.push(() => act(() => root.unmount()));
  await act(async () => {
    root.render(<MemoryTab botId="b1" wire={wire} />);
  });
  const row = container.querySelector('[data-testid="memory-claim"]');
  expect(row?.textContent).toContain("Expired");
  const more = row?.querySelector('[aria-label="More"]') as HTMLElement;
  await act(async () => {
    more.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, button: 0 }));
    more.click();
  });
  const items = [...document.querySelectorAll('[role="menuitem"]')].map((el) => el.textContent);
  expect(items).toEqual(["Forget"]);
});
