// @vitest-environment jsdom

import type { ComponentProps } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

vi.mock("@superchat/ui-web", () => ({
  BotAvatar: () => <div data-testid="bot-avatar" />,
  cn: (...classes: unknown[]) => classes.filter(Boolean).join(" "),
}));

import { EmptyState } from "./index";

async function render(props: ComponentProps<typeof EmptyState>) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(<EmptyState {...props} />);
  });
  return {
    container,
    async cleanup() {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}

it("renders the section's illustration, and never Superchat's orb", async () => {
  const page = await render({ illustration: "trophy", headline: "Nothing yet" });
  try {
    const img = page.container.querySelector("img");
    expect(img?.getAttribute("src")).toBe("/illustrations/trophy.png");
    expect(img?.getAttribute("alt")).toBe("");
    expect(page.container.querySelector('[data-testid="bot-avatar"]')).toBeNull();
  } finally {
    await page.cleanup();
  }
});

it("omits the illustration image when none is given", async () => {
  const page = await render({ headline: "Nothing yet" });
  try {
    expect(page.container.querySelector("img")).toBeNull();
  } finally {
    await page.cleanup();
  }
});
