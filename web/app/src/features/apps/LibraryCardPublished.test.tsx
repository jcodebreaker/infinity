// @vitest-environment jsdom

import type { ReactNode } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../test/i18n")).withI18nRoot(await orig<typeof import("react-dom/client")>()),
);
vi.mock("../../lib/relative-time", () => ({ formatRelativeTime: () => "2h ago" }));
vi.mock("@lingui/react/macro", () => {
  const t = (parts: TemplateStringsArray) => parts.join("");
  return { useLingui: () => ({ t }), Trans: ({ children }: { children: ReactNode }) => children };
});
vi.mock("@lingui/core/macro", () => ({
  t: (parts: TemplateStringsArray) => parts.join(""),
  plural: (n: number, forms: { one: string; other: string }) =>
    (n === 1 ? forms.one : forms.other).replace("#", String(n)),
}));
vi.mock("@superchat/ui-web", () => {
  const Passthrough = ({ children }: { children?: ReactNode }) => <div>{children}</div>;
  return {
    DropdownMenu: Passthrough,
    DropdownMenuContent: Passthrough,
    DropdownMenuItem: Passthrough,
    DropdownMenuTrigger: Passthrough,
  };
});
vi.mock("../artifacts/ArtifactPreviewCard", () => ({
  glassActionClassName: "",
  GlassAction: Object.assign(() => null, {}),
  ArtifactPreviewCard: ({ meta, badge }: { meta: ReactNode; badge: ReactNode }) => (
    <div>
      <span data-testid="meta">{meta}</span>
      {badge}
    </div>
  ),
}));

import { ArtifactRegistryProvider } from "../artifacts";
import { LibraryCard } from "../artifacts/library/LibraryCard";
import { appsExtension } from "./extension";

const base = {
  id: "a-1",
  botId: "bot-1",
  runId: null,
  name: "todo.html",
  description: null,
  mimeType: "text/html",
  size: 512,
  version: 1,
  versionCount: 1,
  createdAt: new Date().toISOString(),
};

async function render(artifact: typeof base & { publish?: unknown }) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <ArtifactRegistryProvider value={[appsExtension]}>
        <LibraryCard
          artifact={artifact as never}
          onOpen={() => {}}
          onDownload={() => {}}
          onDelete={() => {}}
        />
      </ArtifactRegistryProvider>,
    );
  });
  return container;
}

it("marks a published app on its card with its opens", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const card = await render({
    ...base,
    publish: {
      slug: "todo-k3m9xq",
      urlPath: "/apps/todo-k3m9xq",
      audience: "org",
      version: 1,
      publishedAt: "",
      stats: { opensTotal: 14, uniqueViewers: 5, opens7d: 9 },
    },
  });
  const marks = card.querySelectorAll("[data-testid=library-card-published]");
  expect(marks).toHaveLength(1);
  expect(marks[0]?.textContent).toBe("Published");
  expect(card.textContent).toContain("14 opens · 5 viewers");
  vi.unstubAllGlobals();
});

it("leaves an unpublished card unmarked", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const card = await render(base);
  expect(card.querySelector("[data-testid=library-card-published]")).toBeNull();
  expect(card.textContent).not.toContain("opens");
  vi.unstubAllGlobals();
});
