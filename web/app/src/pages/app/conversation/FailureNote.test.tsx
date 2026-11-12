// @vitest-environment jsdom

import type { ThreadMessage } from "@superchat/contracts";
import type { ReactNode } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../../test/i18n")).withI18nRoot(
    await orig<typeof import("react-dom/client")>(),
  ),
);

vi.mock("@lingui/react/macro", () => {
  const t = (parts: TemplateStringsArray, ...values: unknown[]) =>
    parts.reduce((acc, part, i) => `${acc}${part}${values[i] ?? ""}`, "");
  return {
    useLingui: () => ({ t, i18n: { locale: "en" } }),
    Trans: ({ children }: { children: ReactNode }) => children,
  };
});
vi.mock("@lingui/core/macro", () => ({
  plural: (count: number, forms: { one: string; other: string }) =>
    (count === 1 ? forms.one : forms.other).replace("#", String(count)),
}));

import { FailureRun } from "./FailureNote";

const failure = (id: string, code: string): ThreadMessage => ({
  id,
  threadId: "t1",
  seq: 0,
  role: "bot",
  blocks: [{ kind: "error", code }],
  createdAt: "2026-10-07T12:00:00.000Z",
});

it("reads as one line with a count, and lists each note behind Show", async () => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  await act(async () => {
    root.render(
      <FailureRun
        messages={[
          failure("a", "sandbox_unavailable"),
          failure("b", "sandbox_unavailable"),
          failure("c", "timeout"),
        ]}
      />,
    );
  });
  try {
    expect(host.textContent).toContain("3 failed attempts");
    expect(host.querySelectorAll("[data-testid=message-error-note]")).toHaveLength(0);
    const toggle = host.querySelector<HTMLButtonElement>("button[aria-expanded]");
    expect(toggle?.textContent).toBe("Show");
    await act(async () => toggle?.click());
    const notes = [...host.querySelectorAll("[data-testid=message-error-note]")];
    expect(notes.map((note) => note.textContent)).toEqual([
      "My machine restarted. Try again.",
      "My machine restarted. Try again.",
      "That took too long. Try again.",
    ]);
    expect(toggle?.textContent).toBe("Hide");
    expect(toggle?.getAttribute("aria-expanded")).toBe("true");
  } finally {
    await act(async () => root.unmount());
    host.remove();
  }
});
