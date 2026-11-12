// @vitest-environment jsdom

import type { ComponentProps, ReactElement, ReactNode } from "react";
import { act, cloneElement } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

vi.mock("react-dom/client", async (orig) =>
  (await import("../../test/i18n")).withI18nRoot(await orig<typeof import("react-dom/client")>()),
);

const api = vi.hoisted(() => ({
  listSpace: vi.fn(),
  getById: vi.fn(),
  listVersions: vi.fn(),
  remove: vi.fn(),
}));
vi.mock("../../lib/rpc", () => ({ rpc: { artifacts: api } }));
vi.mock("../../lib/artifact-open", () => ({
  decodeArtifactBase64: vi.fn(),
  downloadArtifactBytes: vi.fn(),
}));
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
  const cn = (...args: unknown[]) => args.filter(Boolean).join(" ");
  const Passthrough = ({ children, ...props }: ComponentProps<"div">) => (
    <div {...props}>{children}</div>
  );
  const ButtonMock = (props: ComponentProps<"button">) => <button type="button" {...props} />;
  return {
    cn,
    BotAvatar: () => <div data-testid="bot-avatar" />,
    AlertDialog: ({ open, children }: { open?: boolean; children?: ReactNode }) =>
      open ? <div>{children}</div> : null,
    AlertDialogAction: ButtonMock,
    AlertDialogCancel: ButtonMock,
    AlertDialogContent: Passthrough,
    AlertDialogDescription: Passthrough,
    AlertDialogFooter: Passthrough,
    AlertDialogHeader: Passthrough,
    AlertDialogTitle: Passthrough,
    Button: ButtonMock,
    Dialog: ({ open, children }: { open?: boolean; children?: ReactNode }) =>
      open ? <div>{children}</div> : null,
    DialogContent: Passthrough,
    DialogHeader: Passthrough,
    DialogTitle: Passthrough,
    DropdownMenu: Passthrough,
    DropdownMenuContent: Passthrough,
    DropdownMenuItem: ButtonMock,
    DropdownMenuTrigger: ({ render, children }: { render?: ReactElement; children?: ReactNode }) =>
      render ? (
        cloneElement(render, undefined, children)
      ) : (
        <button type="button">{children}</button>
      ),
    Skeleton: (props: ComponentProps<"div">) => <div {...props} />,
  };
});

import { LibraryScreen } from "./LibraryScreen";

function artifact(overrides: {
  id: string;
  name: string;
  mimeType: string;
  description?: string | null;
}) {
  return {
    id: overrides.id,
    botId: "bot-1",
    runId: null,
    name: overrides.name,
    description: overrides.description ?? null,
    mimeType: overrides.mimeType,
    size: 512,
    version: 1,
    versionCount: 1,
    createdAt: new Date().toISOString(),
  };
}

function typeSearch(container: HTMLElement, value: string) {
  const input = container.querySelector("input[type='search']");
  if (!(input instanceof HTMLInputElement)) throw new Error("missing search input");
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  setter?.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

function findButton(container: HTMLElement, text: string) {
  return [...container.querySelectorAll("button")].find((button) =>
    button.textContent?.includes(text),
  );
}

async function renderLibrary(onSendIdea: (text: string) => void = vi.fn()) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(<LibraryScreen botId="bot-1" onSendIdea={onSendIdea} onOpenArtifact={vi.fn()} />);
  });
  return {
    container,
    async cleanup() {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}

it("lists what listSpace returns and shows a facet chip per kind with counts", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  api.listSpace.mockResolvedValue({
    items: [
      artifact({ id: "a-1", name: "Trip itinerary", mimeType: "text/html" }),
      artifact({ id: "a-2", name: "Q3 report", mimeType: "application/pdf" }),
      artifact({ id: "a-3", name: "Sunset photo", mimeType: "image/png" }),
    ],
    nextCursor: null,
  });
  const page = await renderLibrary();
  try {
    await act(async () => {
      await vi.waitFor(() => {
        expect(page.container.textContent).toContain("Trip itinerary");
      });
    });
    expect(page.container.textContent).toContain("Q3 report");
    expect(page.container.textContent).toContain("Sunset photo");
    expect(findButton(page.container, "All 3")).toBeTruthy();
    expect(findButton(page.container, "Pages 1")).toBeTruthy();
    expect(findButton(page.container, "Documents 1")).toBeTruthy();
    expect(findButton(page.container, "Images 1")).toBeTruthy();
  } finally {
    await page.cleanup();
    vi.unstubAllGlobals();
  }
});

it("filters by name and description as the search field is typed", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  api.listSpace.mockResolvedValue({
    items: [
      artifact({ id: "a-1", name: "Trip itinerary", mimeType: "text/html" }),
      artifact({
        id: "a-2",
        name: "Q3 report",
        mimeType: "application/pdf",
        description: "Quarterly numbers",
      }),
    ],
    nextCursor: null,
  });
  const page = await renderLibrary();
  try {
    await act(async () => {
      await vi.waitFor(() => {
        expect(page.container.textContent).toContain("Trip itinerary");
      });
    });
    act(() => {
      typeSearch(page.container, "quarterly");
    });
    // The card reads its saved title, not its file name.
    expect(page.container.textContent).toContain("Quarterly numbers");
    expect(page.container.textContent).not.toContain("Trip itinerary");
  } finally {
    await page.cleanup();
    vi.unstubAllGlobals();
  }
});

it("filters to one kind when its facet chip is selected", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  api.listSpace.mockResolvedValue({
    items: [
      artifact({ id: "a-1", name: "Trip itinerary", mimeType: "text/html" }),
      artifact({ id: "a-2", name: "Q3 report", mimeType: "application/pdf" }),
    ],
    nextCursor: null,
  });
  const page = await renderLibrary();
  try {
    await act(async () => {
      await vi.waitFor(() => {
        expect(page.container.textContent).toContain("Trip itinerary");
      });
    });
    const pagesChip = findButton(page.container, "Pages 1");
    expect(pagesChip).toBeTruthy();
    act(() => {
      pagesChip?.click();
    });
    expect(page.container.textContent).toContain("Trip itinerary");
    expect(page.container.textContent).not.toContain("Q3 report");

    const allChip = findButton(page.container, "All 2");
    act(() => {
      allChip?.click();
    });
    expect(page.container.textContent).toContain("Q3 report");
  } finally {
    await page.cleanup();
    vi.unstubAllGlobals();
  }
});

it("shows the empty state when the Library has nothing yet", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  api.listSpace.mockResolvedValue({ items: [], nextCursor: null });
  const page = await renderLibrary();
  try {
    await act(async () => {
      await vi.waitFor(() => {
        expect(page.container.textContent).toContain("Nothing here yet.");
      });
    });
    expect(page.container.textContent).toContain(
      "Apps, decks, documents and files your Muse makes will appear here",
    );
  } finally {
    await page.cleanup();
    vi.unstubAllGlobals();
  }
});

it("tapping an empty-state suggestion chip calls onSendIdea with its text", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  api.listSpace.mockResolvedValue({ items: [], nextCursor: null });
  const onSendIdea = vi.fn();
  const page = await renderLibrary(onSendIdea);
  try {
    await act(async () => {
      await vi.waitFor(() => {
        expect(page.container.textContent).toContain("Make a short deck about our new product");
      });
    });
    const button = findButton(page.container, "Make a short deck about our new product");
    expect(button).toBeTruthy();
    await act(async () => {
      button?.click();
    });
    expect(onSendIdea).toHaveBeenCalledWith("Make a short deck about our new product");
  } finally {
    await page.cleanup();
    vi.unstubAllGlobals();
  }
});

it("shows a no-results state when the search matches nothing", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  api.listSpace.mockResolvedValue({
    items: [artifact({ id: "a-1", name: "Trip itinerary", mimeType: "text/html" })],
    nextCursor: null,
  });
  const page = await renderLibrary();
  try {
    await act(async () => {
      await vi.waitFor(() => {
        expect(page.container.textContent).toContain("Trip itinerary");
      });
    });
    act(() => {
      typeSearch(page.container, "nonexistent-xyz");
    });
    expect(page.container.textContent).toContain("Nothing matches your search.");
  } finally {
    await page.cleanup();
    vi.unstubAllGlobals();
  }
});

it("adds a Published facet that filters to the published apps", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  api.listSpace.mockResolvedValue({
    items: [
      {
        ...artifact({ id: "a-1", name: "todo.html", mimeType: "text/html" }),
        publish: {
          slug: "todo-k3m9xq",
          urlPath: "/apps/todo-k3m9xq",
          audience: "org",
          version: 1,
          publishedAt: "",
          stats: { opensTotal: 14, uniqueViewers: 5, opens7d: 9 },
        },
      },
      artifact({ id: "a-2", name: "notes.html", mimeType: "text/html" }),
    ],
    nextCursor: null,
  });
  const page = await renderLibrary();
  try {
    await act(async () => {
      await vi.waitFor(() => {
        expect(page.container.textContent).toContain("Todo");
      });
    });
    const facet = findButton(page.container, "Published 1");
    expect(facet).toBeTruthy();
    act(() => {
      facet?.click();
    });
    expect(page.container.textContent).toContain("Todo");
    expect(page.container.textContent).not.toContain("Notes");
  } finally {
    await page.cleanup();
    vi.unstubAllGlobals();
  }
});

it("hides the kind filter when every item is the same kind", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  api.listSpace.mockResolvedValue({
    items: [
      artifact({ id: "a-1", name: "tea.deck.html", mimeType: "text/html" }),
      artifact({ id: "a-2", name: "coffee.deck.html", mimeType: "text/html" }),
    ],
    nextCursor: null,
  });
  const page = await renderLibrary();
  try {
    await act(async () => {
      await vi.waitFor(() => {
        expect(page.container.textContent).toContain("Coffee");
      });
    });
    expect(findButton(page.container, "All 2")).toBeUndefined();
    expect(findButton(page.container, "Decks 2")).toBeUndefined();
    expect(page.container.querySelector("input[type='search']")).toBeTruthy();
  } finally {
    await page.cleanup();
    vi.unstubAllGlobals();
  }
});

it("titles a card with its saved title, else its file name made readable", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  api.listSpace.mockResolvedValue({
    items: [
      artifact({
        id: "a-1",
        name: "tea.deck.html",
        mimeType: "text/html",
        description: "Tea: one plant, many teas",
      }),
      artifact({ id: "a-2", name: "q3_sales-report.pdf", mimeType: "application/pdf" }),
    ],
    nextCursor: null,
  });
  const page = await renderLibrary();
  try {
    await act(async () => {
      await vi.waitFor(() => {
        expect(page.container.textContent).toContain("Tea: one plant, many teas");
      });
    });
    expect(page.container.textContent).toContain("Q3 sales report");
    expect(page.container.textContent).not.toContain("tea.deck.html");
    expect(page.container.querySelector("[aria-label='Open Q3 sales report']")).toBeTruthy();
  } finally {
    await page.cleanup();
    vi.unstubAllGlobals();
  }
});

it("in a dock sheet, shows one compact search beside the filter chips", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  api.listSpace.mockResolvedValue({
    items: [
      artifact({ id: "a-1", name: "Trip itinerary", mimeType: "text/html" }),
      artifact({ id: "a-2", name: "Q3 report", mimeType: "application/pdf" }),
    ],
    nextCursor: null,
  });
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(<LibraryScreen botId="bot-1" onOpenArtifact={vi.fn()} />);
  });
  try {
    await act(async () => {
      await vi.waitFor(() => {
        expect(container.textContent).toContain("Trip itinerary");
      });
    });
    expect(container.querySelectorAll("input[type='search']")).toHaveLength(1);
    const pages = findButton(container, "Pages 1");
    expect(pages?.getAttribute("aria-pressed")).toBe("false");
    act(() => {
      pages?.click();
    });
    expect(findButton(container, "Pages 1")?.getAttribute("aria-pressed")).toBe("true");
    expect(container.textContent).not.toContain("Q3 report");
  } finally {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  }
});
