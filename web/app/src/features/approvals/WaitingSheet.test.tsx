// @vitest-environment jsdom

import type { Ask } from "@superchat/contracts";
import type { ComponentProps, ReactNode } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../test/i18n")).withI18nRoot(await orig<typeof import("react-dom/client")>()),
);

const api = vi.hoisted(() => ({ list: vi.fn(), count: vi.fn(), answer: vi.fn() }));
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
  Dialog: ({ open, children }: { open: boolean; children?: ReactNode }) =>
    open ? <div>{children}</div> : null,
  DialogContent: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children?: ReactNode }) => <h2>{children}</h2>,
  DialogDescription: ({ children }: { children?: ReactNode }) => <p>{children}</p>,
  BotAvatar: () => <span data-testid="muse-face" />,
  Button: (props: ComponentProps<"button">) => <button {...props} />,
  Input: (props: ComponentProps<"input">) => <input {...props} />,
  cn: (...classes: unknown[]) => classes.filter(Boolean).join(" "),
}));

import { WaitingSheet } from "./WaitingSheet";

function ask(overrides: Partial<Ask>): Ask {
  return {
    id: "ask-1",
    runId: "run-1",
    kind: "question",
    goalId: null,
    goalTitle: null,
    text: "Which evenings work?",
    choices: [{ id: "mon", label: "Monday" }],
    input: null,
    createdAt: new Date("2026-01-01T00:00:00Z").toISOString(),
    ...overrides,
  };
}

async function renderSheet() {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const onOpenChange = vi.fn();
  await act(async () => {
    root.render(<WaitingSheet botId="bot-1" open onOpenChange={onOpenChange} />);
  });
  return {
    container,
    onOpenChange,
    async cleanup() {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}

it("lists the sample Asks, newest first", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  api.list.mockResolvedValue([
    ask({
      id: "ask-old",
      text: "Older ask",
      createdAt: new Date("2026-01-01T00:00:00Z").toISOString(),
    }),
    ask({
      id: "ask-new",
      text: "Newer ask",
      createdAt: new Date("2026-01-02T00:00:00Z").toISOString(),
    }),
  ]);
  const page = await renderSheet();
  try {
    await act(async () => {
      await vi.waitFor(() => {
        expect(page.container.textContent).toContain("Older ask");
      });
    });
    const olderIndex = page.container.textContent?.indexOf("Older ask") ?? -1;
    const newerIndex = page.container.textContent?.indexOf("Newer ask") ?? -1;
    expect(newerIndex).toBeGreaterThanOrEqual(0);
    expect(newerIndex).toBeLessThan(olderIndex);
  } finally {
    await page.cleanup();
    vi.unstubAllGlobals();
  }
});

it("splits the Inbox into Approvals and Questions", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  api.list.mockResolvedValue([
    ask({ id: "ask-approval", kind: "approval", text: "Send the email" }),
    ask({ id: "ask-question", kind: "question", text: "Which evenings work?" }),
  ]);
  const page = await renderSheet();
  try {
    await act(async () => {
      await vi.waitFor(() => {
        expect(page.container.textContent).toContain("Which evenings work?");
      });
    });
    const tab = (label: string) =>
      [...page.container.querySelectorAll("button")].find((button) =>
        button.textContent?.startsWith(label),
      );
    await act(async () => {
      tab("Questions")?.click();
    });
    const open = () => page.container.querySelector("[data-testid='waiting-open']")?.textContent;
    expect(open()).toContain("Which evenings work?");
    expect(open()).not.toContain("Send the email");
    await act(async () => {
      tab("Approvals")?.click();
    });
    expect(open()).toContain("Send the email");
    expect(open()).not.toContain("Which evenings work?");
  } finally {
    await page.cleanup();
    vi.unstubAllGlobals();
  }
});

it("answering a choice calls asks.answer with {askId, runId, answer} and removes it", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  let current: Ask[] = [ask({ id: "ask-1", runId: "run-1", text: "Which evenings work?" })];
  api.list.mockImplementation(async () => current);
  api.answer.mockImplementation(async (input: { askId: string }) => {
    current = current.filter((item) => item.id !== input.askId);
    return { ok: true };
  });
  const page = await renderSheet();
  try {
    await act(async () => {
      await vi.waitFor(() => {
        expect(page.container.textContent).toContain("Which evenings work?");
      });
    });
    const choiceButton = [...page.container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("Monday"),
    );
    expect(choiceButton).toBeTruthy();
    await act(async () => {
      choiceButton?.click();
    });
    await act(async () => {
      await vi.waitFor(() => {
        const open = page.container.querySelector("[data-testid='waiting-open']");
        expect(open?.textContent).not.toContain("Which evenings work?");
      });
    });
    expect(api.answer).toHaveBeenCalledWith({ askId: "ask-1", runId: "run-1", answer: "mon" });
    // It moves to this session's Decided list, with the choice made.
    const decided = page.container.querySelector("[data-testid='waiting-decided']");
    expect(decided?.textContent).toContain("Which evenings work?");
    expect(decided?.textContent).toContain("Monday");
  } finally {
    await page.cleanup();
    vi.unstubAllGlobals();
  }
});

it("sends the typed text for text-input Asks", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  let current: Ask[] = [
    ask({
      id: "ask-text",
      runId: "run-2",
      text: "What should we name the plan?",
      choices: [],
      input: "text",
    }),
  ];
  api.list.mockImplementation(async () => current);
  api.answer.mockImplementation(async (input: { askId: string }) => {
    current = current.filter((item) => item.id !== input.askId);
    return { ok: true };
  });
  const page = await renderSheet();
  try {
    await act(async () => {
      await vi.waitFor(() => {
        expect(page.container.textContent).toContain("What should we name the plan?");
      });
    });
    const input = page.container.querySelector("input");
    expect(input).toBeTruthy();
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    await act(async () => {
      setter?.call(input, "Kyoto trip prep");
      input?.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const sendButton = [...page.container.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("Send"),
    );
    await act(async () => {
      sendButton?.click();
    });
    await act(async () => {
      await vi.waitFor(() => {
        expect(api.answer).toHaveBeenCalledWith({
          askId: "ask-text",
          runId: "run-2",
          answer: "Kyoto trip prep",
        });
      });
    });
  } finally {
    await page.cleanup();
    vi.unstubAllGlobals();
  }
});

it("shows a short empty state when nothing is waiting", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  api.list.mockResolvedValue([]);
  const page = await renderSheet();
  try {
    await act(async () => {
      await vi.waitFor(() => {
        expect(page.container.textContent).toContain(
          "When I need a decision or an answer, it'll show up here.",
        );
      });
    });
  } finally {
    await page.cleanup();
    vi.unstubAllGlobals();
  }
});
