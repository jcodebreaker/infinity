// @vitest-environment jsdom

import type { Ask } from "@superchat/contracts";
import type { ComponentProps, ReactNode } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../test/i18n")).withI18nRoot(await orig<typeof import("react-dom/client")>()),
);

const api = vi.hoisted(() => ({ list: vi.fn(), answer: vi.fn() }));
vi.mock("../../lib/rpc", () => ({ rpc: { asks: api } }));
vi.mock("../../lib/relative-time", () => ({ formatRelativeTime: () => "just now" }));
vi.mock("@lingui/core/macro", () => ({
  t: (parts: TemplateStringsArray, ...values: unknown[]) =>
    parts.reduce((acc, part, i) => `${acc}${part}${values[i] ?? ""}`, ""),
}));
vi.mock("@lingui/react/macro", () => {
  const t = (parts: TemplateStringsArray, ...values: unknown[]) =>
    parts.reduce((acc, part, i) => `${acc}${part}${values[i] ?? ""}`, "");
  return {
    useLingui: () => ({ t, i18n: { locale: "en" } }),
    Trans: ({ children }: { children: ReactNode }) => children,
  };
});
vi.mock("@superchat/chat-ui/web", () => ({
  ChatMarkdown: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
}));
vi.mock("@superchat/ui-web", () => ({
  Button: (props: ComponentProps<"button">) => <button {...props} />,
  Input: (props: ComponentProps<"input">) => <input {...props} />,
  cn: (...classes: unknown[]) => classes.filter(Boolean).join(" "),
}));

import { ApprovalCards } from "./ApprovalCards";

function approval(id: string, text: string): Ask {
  return {
    id,
    runId: "sess-1",
    kind: "approval",
    goalId: null,
    goalTitle: null,
    text,
    detail: "Always allow: Send messages to bob@acme.test",
    choices: [
      { id: "once", label: "Allow once" },
      { id: "always", label: "Always allow" },
      { id: "deny", label: "Deny" },
    ],
    input: null,
    createdAt: new Date("2026-01-01T00:00:00Z").toISOString(),
    approval: {},
  };
}

async function mount() {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(<ApprovalCards botId="bot-1" />);
  });
  return {
    container,
    async cleanup() {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}

it("shows only approvals and locks the card with the choice", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  api.list.mockResolvedValue([
    approval("elicit_1", "Send an email to bob@acme.test"),
    { ...approval("task_1", "Delete old notes"), kind: "blocked_task", approval: undefined },
  ]);
  api.answer.mockResolvedValue({ ok: true });
  const page = await mount();
  try {
    await act(async () => {
      await vi.waitFor(() =>
        expect(page.container.textContent).toContain("Send an email to bob@acme.test"),
      );
    });
    expect(page.container.textContent).not.toContain("Delete old notes");
    const allowOnce = [...page.container.querySelectorAll("button")].find(
      (button) => button.textContent === "Allow once",
    );
    await act(async () => allowOnce?.click());
    expect(api.answer).toHaveBeenCalledWith({
      askId: "elicit_1",
      runId: "sess-1",
      answer: "once",
    });
    expect(page.container.querySelectorAll("button")).toHaveLength(0);
    expect(page.container.textContent).toContain("Allow once");
    expect(page.container.textContent).toContain("Send an email to bob@acme.test");
  } finally {
    await page.cleanup();
    vi.unstubAllGlobals();
  }
});

it("renders nothing when no approval is waiting here", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  api.list.mockResolvedValue([
    { ...approval("task_1", "Delete old notes"), kind: "blocked_task", approval: undefined },
  ]);
  const page = await mount();
  try {
    await act(async () => {
      await vi.waitFor(() => expect(api.list).toHaveBeenCalled());
    });
    expect(page.container.textContent).toBe("");
  } finally {
    await page.cleanup();
    vi.unstubAllGlobals();
  }
});

it("keeps the call's arguments behind Details and lists the other asks one Allow covers", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const args = '{"command": "rm notes.txt # Rename notes.txt"}';
  api.list.mockResolvedValue([
    {
      ...approval("elicit_3", "Over budget. Continue? (sys_os_shell)"),
      approval: { arguments: args, alsoAsks: ["Pushes need a look."] },
    },
  ]);
  const view = await mount();

  expect(view.container.querySelector("h3")?.textContent).toBe(
    "Over budget. Continue? (sys_os_shell)",
  );
  expect(view.container.querySelector("details")?.open).toBe(false);
  expect(view.container.querySelector("summary")?.textContent).toBe("Details");
  expect(view.container.querySelector("details pre")?.textContent).toBe(args);
  expect(view.container.querySelector("li")?.textContent).toBe("Pushes need a look.");
  await view.cleanup();
});
