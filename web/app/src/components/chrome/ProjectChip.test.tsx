// @vitest-environment jsdom

import type { ReactNode } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../test/i18n")).withI18nRoot(await orig<typeof import("react-dom/client")>()),
);

vi.mock("@lingui/react/macro", () => ({
  Trans: ({ children }: { children: ReactNode }) => children,
}));

import { type ChatProject, ProjectChip, useChatProject } from "./ProjectChip";

const DECK: ChatProject = { slug: "q3-deck", name: "Q3 board deck" };

it("says which Project is open and opens it on click", async () => {
  const onOpen = vi.fn();
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(<ProjectChip project={DECK} onOpen={onOpen} />));

  const chip = container.querySelector<HTMLButtonElement>("[data-testid=project-chip]");
  expect(chip?.textContent).toBe("Working in Q3 board deck");
  await act(async () => chip?.click());
  expect(onOpen).toHaveBeenCalledWith(DECK);
  await act(async () => root.unmount());
});

function Probe({
  load,
  chat,
  refresh,
}: {
  load: () => Promise<{ project: ChatProject | null }>;
  chat: string;
  refresh: number;
}) {
  const project = useChatProject(load, chat, refresh);
  return <span data-testid="probe">{project?.name ?? "none"}</span>;
}

it("follows the chat's Project: reloads after a turn and never shows another chat's", async () => {
  const load = vi
    .fn<() => Promise<{ project: ChatProject | null }>>()
    .mockResolvedValueOnce({ project: null })
    .mockResolvedValueOnce({ project: DECK })
    .mockImplementation(() => new Promise(() => undefined));
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const text = () => container.querySelector("[data-testid=probe]")?.textContent;

  await act(async () => root.render(<Probe load={load} chat="a" refresh={0} />));
  expect(text()).toBe("none");
  await act(async () => root.render(<Probe load={load} chat="a" refresh={1} />));
  expect(text()).toBe("Q3 board deck");
  await act(async () => root.render(<Probe load={load} chat="b" refresh={1} />));
  expect(text()).toBe("none");
  await act(async () => root.unmount());
});
