// @vitest-environment jsdom

import { i18n } from "@lingui/core";
import { I18nProvider } from "@lingui/react";
import type { ComponentProps, ReactNode } from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

// jsdom does not implement these; Shell (and the real @superchat/ui-web components
// it renders) use them unconditionally on mount.
beforeEach(() => {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: false,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  }));
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
    return setTimeout(() => cb(0), 0) as unknown as number;
  });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => clearTimeout(id));
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  const elementProto = Element.prototype as unknown as {
    scrollTo?: (...args: unknown[]) => void;
    scrollIntoView?: (...args: unknown[]) => void;
  };
  elementProto.scrollTo = () => undefined;
  elementProto.scrollIntoView = () => undefined;
  vi.spyOn(performance, "mark").mockImplementation(() => undefined as unknown as PerformanceMark);
  vi.spyOn(performance, "getEntriesByName").mockImplementation(() => []);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const bootstrapApi = vi.hoisted(() => ({ takeInitialBootstrap: vi.fn(), loadMe: vi.fn() }));
vi.mock("../lib/bootstrap", () => bootstrapApi);

/** What the engine's Conversation answers; the other calls never resolve. */
const engine = vi.hoisted(() => ({
  transcript: vi.fn(),
  send: vi.fn(),
  interrupt: vi.fn(),
}));

vi.mock("../lib/rpc", () => {
  function pendingRpc(): unknown {
    const target = () => new Promise(() => undefined);
    return new Proxy(target, { get: () => pendingRpc() });
  }
  const chats = new Proxy(engine as Record<string, unknown>, {
    get: (target, key) => (typeof key === "string" && key in target ? target[key] : pendingRpc()),
  });
  return {
    rpc: new Proxy({}, { get: (_target, key) => (key === "chats" ? chats : pendingRpc()) }),
  };
});

function tagText(strings: TemplateStringsArray, ...values: unknown[]) {
  return strings.reduce(
    (acc, part, i) => acc + part + (values[i] !== undefined ? String(values[i]) : ""),
    "",
  );
}
vi.mock("@lingui/core/macro", () => ({ t: tagText }));
vi.mock("@lingui/react/macro", () => ({
  useLingui: () => ({ t: tagText, i18n: {} }),
  Trans: ({ children }: { children?: ReactNode }) => children,
}));

vi.mock("@superchat/chat-ui/web", () => ({
  ChatMarkdown: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
}));

vi.mock("@superchat/ui-web", () => {
  // Passes every prop straight onto a <div> (event handlers included) so
  // data-testid/aria-label/onClick still work for assertions and clicks even
  // though this isn't the real Base UI component.
  const Passthrough = ({ children, ...rest }: ComponentProps<"div">) => (
    <div {...rest}>{children}</div>
  );
  return {
    AvatarStyleProvider: ({ children }: { children?: ReactNode }) => <>{children}</>,
    BotAvatar: ({ identity }: { identity?: string }) => (
      <span data-testid="bot-avatar" data-identity={identity} />
    ),
    Button: (props: ComponentProps<"button">) => <button {...props} />,
    Dialog: ({ children }: { children?: ReactNode }) => <>{children}</>,
    DialogContent: Passthrough,
    DialogDescription: Passthrough,
    DialogTitle: Passthrough,
    cn: (...parts: unknown[]) => parts.filter(Boolean).join(" "),
    DropdownMenu: ({ children }: { children?: ReactNode }) => <>{children}</>,
    DropdownMenuContent: Passthrough,
    DropdownMenuItem: (props: ComponentProps<"div">) => <div {...props} />,
    DropdownMenuTrigger: Passthrough,
    Sheet: ({ open, children }: { open?: boolean; children?: ReactNode }) =>
      open ? <>{children}</> : null,
    SheetContent: Passthrough,
    Skeleton: Passthrough,
    SheetTitle: Passthrough,
    SheetDescription: Passthrough,
    Tooltip: ({ children }: { children?: ReactNode }) => <>{children}</>,
    TooltipContent: () => null,
    TooltipTrigger: (props: ComponentProps<"button">) => <button type="button" {...props} />,
    InputGroup: Passthrough,
    InputGroupAddon: Passthrough,
    InputGroupInput: (props: ComponentProps<"input">) => <input {...props} />,
    Popover: ({ children }: { children?: ReactNode }) => <>{children}</>,
    PopoverContent: Passthrough,
    PopoverTrigger: Passthrough,
    resolvePersonaColorDef: () => ({ hex: "#000000" }),
    // Real behavior (web/packages/ui-web/src/bot-avatar.tsx): museState.ts (Shell's live
    // Muse state derivation) calls this directly, so the mock must actually derive.
    museAvatarState: (status: string | undefined, waitingCount: number | undefined) => {
      if ((waitingCount ?? 0) > 0) return "waiting";
      if (status === "queued" || status === "leased") return "thinking";
      if (status === "running" || status === "waiting_input") {
        return "working";
      }
      return "idle";
    },
  };
});

// Stubs: every module below is presentational chrome not under test here
// (settings panels, dialogs, message cards, ...). Each export
// becomes either a component rendering nothing or a harmless no-op function.
function Stub() {
  return null;
}

vi.mock("../features/artifacts/ArtifactFileCard", () => ({ ArtifactFileCard: Stub }));
vi.mock("../features/approvals/AskCard", () => ({ AskCard: Stub }));
vi.mock("../components/CloudAgentCard", () => ({ CloudAgentCard: Stub }));
vi.mock("../components/MessageHoverMetadata", () => ({ MessageHoverMetadata: Stub }));
vi.mock("./app/sheets/DockSheets", () => ({ DockSheets: Stub }));
vi.mock("../features/projects/WorkIn", () => ({
  useChatPlace: () => ({ place: null, reload: () => undefined }),
  WorkInChip: Stub,
}));
vi.mock("./SettingsOverlay", () => ({ SettingsOverlay: Stub }));
vi.mock("./shell/dialogs", () => ({ ClearConversationDialog: Stub }));
vi.mock("./app/conversation/message-cards", () => ({
  ArtifactImage: Stub,
}));

import { ShellPage } from "./Shell";

function bot(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    id: "bot-1",
    name: "My Muse",
    title: "My Muse",
    description: "",
    color: "#0090FF",
    unread: false,
    preview: "",
    status: "idle",
    updatedAt: new Date().toISOString(),
    createdAt: new Date().toISOString(),
    ...overrides,
  };
}

function me(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    userId: "user-1",
    name: "Person",
    isAdmin: false,
    needsModel: false,
    defaultProvider: null,
    defaultModel: null,
    avatarStyle: "robot",
    timezone: "UTC",
    ...overrides,
  };
}

function bootstrap(overrides: Partial<Record<string, unknown>> = {}) {
  return { me: me(), bots: [bot()], ...overrides };
}

async function renderShell(initialBootstrap: ReturnType<typeof bootstrap>, entry = "/app") {
  bootstrapApi.takeInitialBootstrap.mockResolvedValue(initialBootstrap);
  bootstrapApi.loadMe.mockResolvedValue(initialBootstrap.me);
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  i18n.loadAndActivate({ locale: "en", messages: {} });
  await act(async () => {
    root.render(
      <I18nProvider i18n={i18n}>
        <MemoryRouter initialEntries={[entry]}>
          <Routes>
            <Route path="/app" element={<ShellPage />} />
            <Route path="/app/:botId" element={<ShellPage />} />
          </Routes>
        </MemoryRouter>
      </I18nProvider>,
    );
  });
  // Let the bootstrap promise resolve and its state updates flush.
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  return {
    container,
    async cleanup() {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}

beforeEach(() => {
  const stored = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => stored.get(key) ?? null,
    setItem: (key: string, value: string) => stored.set(key, value),
    removeItem: (key: string) => stored.delete(key),
  });
  engine.transcript.mockReset().mockImplementation(() => new Promise(() => undefined));
  engine.send.mockReset().mockResolvedValue({ ok: true });
  engine.interrupt.mockReset().mockResolvedValue({ delivered: true });
});

it("opens on Home with the dock, no sidebar or chat list, no Goals, Feed or Ideas", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const page = await renderShell(bootstrap({ me: me() }));
  try {
    // Home is the first screen on every load, before the Conversation has even loaded.
    expect(page.container.querySelector('[data-testid="home"]')).toBeTruthy();
    const dock = page.container.querySelector('[data-testid="home"] [data-testid="dock"]');
    const dockButtons = [...(dock?.querySelectorAll("button") ?? [])];
    expect(dockButtons.map((el) => el.getAttribute("aria-label"))).toEqual([
      "Conversation",
      "Topics",
      "Inbox",
      "Library",
      "Automations",
      "Files",
    ]);
    // On Home the Conversation is not the screen, so its dock button is not pressed.
    expect(dockButtons[0]?.getAttribute("aria-pressed")).toBe("false");
    expect(page.container.querySelector('[data-testid="chats-trigger"]')).toBeNull();
    expect(page.container.querySelector('[data-testid="app-rail"]')).toBeNull();
    const text = page.container.textContent ?? "";
    for (const gone of ["Goals", "Feed", "Ideas", "Superchat"]) expect(text).not.toContain(gone);
    expect(page.container.querySelector('[data-testid="bots-sidebar"]')).toBeNull();
  } finally {
    await page.cleanup();
  }
});

const turn = [
  {
    id: "m1",
    threadId: "conv",
    seq: 1,
    role: "user",
    blocks: [{ kind: "text", text: "find the invoice" }],
    createdAt: "2026-10-10T10:00:00.000Z",
  },
];

it("shows the Muse working from the engine's transcript, and Stop interrupts the engine", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  engine.transcript.mockResolvedValue({
    threadId: "conv",
    messages: turn,
    olderCursor: null,
    running: true,
  });
  const page = await renderShell(bootstrap());
  try {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(engine.transcript).toHaveBeenCalledWith({ botId: "bot-1" });
    expect(page.container.querySelector('[data-testid="muse-live-row"]')).toBeTruthy();
    const stop = page.container.querySelector('button[aria-label="Stop"]') as HTMLElement;
    expect(stop).toBeTruthy();
    await act(async () => stop.click());
    expect(engine.interrupt).toHaveBeenCalledWith({ botId: "bot-1" });
  } finally {
    await page.cleanup();
  }
});

it("shows no working row and no Stop when the engine is idle", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  engine.transcript.mockResolvedValue({
    threadId: "conv",
    messages: [
      ...turn,
      { ...turn[0], id: "m2", role: "bot", blocks: [{ kind: "text", text: "Found it." }] },
    ],
    olderCursor: null,
    running: false,
  });
  const page = await renderShell(bootstrap());
  try {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(page.container.querySelector('[data-testid="muse-live-row"]')).toBeNull();
    expect(page.container.querySelector('button[aria-label="Stop"]')).toBeNull();
    expect(page.container.textContent).toContain("find the invoice");
  } finally {
    await page.cleanup();
  }
});

it("sends to the engine's Conversation and shows the message at once", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  engine.transcript.mockResolvedValue({
    threadId: "conv",
    messages: turn,
    olderCursor: null,
    running: false,
  });
  engine.send.mockImplementation(() => new Promise(() => undefined));
  const page = await renderShell(bootstrap());
  try {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    const box = page.container.querySelector("textarea") as HTMLTextAreaElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(
        box,
        "and the receipt?",
      );
      box.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => {
      box.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    });
    expect(engine.send).toHaveBeenCalledWith({
      botId: "bot-1",
      text: "and the receipt?",
      attachments: undefined,
      folders: undefined,
    });
    // The engine has not recorded it yet, and the person already sees it.
    expect(page.container.textContent).toContain("and the receipt?");
    expect(page.container.querySelector('[data-testid="muse-live-row"]')).toBeTruthy();
  } finally {
    await page.cleanup();
  }
});

it("reads one work state: the header pill, the working row and Stop light and settle together", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  engine.transcript.mockResolvedValue({
    threadId: "conv",
    messages: [
      {
        id: "u1",
        threadId: "conv",
        seq: 0,
        role: "user",
        blocks: [{ kind: "text", text: "make the deck" }],
        createdAt: new Date().toISOString(),
      },
    ],
    olderCursor: null,
    running: true,
  });
  const page = await renderShell(bootstrap());
  const lit = () => ({
    pill: Boolean(page.container.querySelector('[data-testid="working-pill"]')),
    row: Boolean(page.container.querySelector('[data-testid="muse-live-row"]')),
    stop: Boolean(page.container.querySelector('button[aria-label="Stop"]')),
  });
  try {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(lit()).toEqual({ pill: true, row: true, stop: true });
    await act(async () => {
      (page.container.querySelector('button[aria-label="Stop"]') as HTMLButtonElement).click();
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(engine.interrupt).toHaveBeenCalledWith({ botId: "bot-1" });
    // The engine still says its turn runs; the Stop landed, so nothing reads as running.
    expect(lit()).toEqual({ pill: false, row: false, stop: false });
  } finally {
    await page.cleanup();
  }
});

const exchange = [
  ...turn,
  {
    ...turn[0],
    id: "m2",
    seq: 2,
    role: "bot",
    blocks: [{ kind: "text", text: "**Found it** in your mail. It was sent on Tuesday." }],
  },
];

async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 20));
  });
}

it("opens on Home, and Continue the conversation slides into the Conversation at its composer", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  engine.transcript.mockResolvedValue({
    threadId: "conv",
    messages: exchange,
    olderCursor: null,
    running: false,
  });
  const page = await renderShell(bootstrap());
  try {
    await settle();
    const home = page.container.querySelector('[data-testid="home"]');
    expect(home).toBeTruthy();
    // The Conversation under Home is out of reach of the keyboard.
    const chat = page.container.querySelector('[data-testid="conversation-layer"]');
    expect(chat?.hasAttribute("inert")).toBe(true);
    const pill = page.container.querySelector(
      '[data-testid="continue-conversation"]',
    ) as HTMLButtonElement;
    expect(pill.tagName).toBe("BUTTON");
    // The reply's first sentence, without its Markdown.
    expect(pill.textContent).toContain("Continue the conversation");
    expect(pill.textContent).toContain("Last: Found it in your mail.");
    expect(pill.textContent).not.toContain("Tuesday");
    await act(async () => pill.click());
    await settle();
    expect(page.container.querySelector('[data-testid="home"]')).toBeNull();
    expect(chat?.hasAttribute("inert")).toBe(false);
    expect(document.activeElement?.tagName).toBe("TEXTAREA");
    expect(chat?.contains(document.activeElement)).toBe(true);
    // The logo is Home (no text Home button any more; the pull's tab shows only while pulling):
    // it slides Home back, with the composer.
    const textHome = [...page.container.querySelectorAll("button")].filter(
      (el) => el.textContent === "Home",
    );
    expect(textHome.map((el) => el.dataset.testid)).toEqual(["pull-home"]);
    expect(textHome[0]?.getAttribute("aria-hidden")).toBe("true");
    const logos = [
      ...page.container.querySelectorAll<HTMLButtonElement>('[data-testid="logo-home"]'),
    ];
    expect(logos.map((logo) => logo.getAttribute("aria-label"))).toEqual(["Home", "Home"]);
    await act(async () => logos[0]?.click());
    await settle();
    const back = page.container.querySelector('[data-testid="home"]');
    expect(back).toBeTruthy();
    expect(back?.contains(document.activeElement)).toBe(true);
  } finally {
    await page.cleanup();
  }
});

it("goes between Home and the Conversation with ⌘↓ / ⌘↑ and a pull past the top", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  engine.transcript.mockResolvedValue({
    threadId: "conv",
    messages: exchange,
    olderCursor: null,
    running: false,
  });
  const page = await renderShell(bootstrap());
  const key = async (name: string, target: EventTarget = document.body) => {
    await act(async () => {
      target.dispatchEvent(
        new KeyboardEvent("keydown", { key: name, metaKey: true, bubbles: true }),
      );
    });
    await settle();
  };
  const home = () => page.container.querySelector('[data-testid="home"]');
  try {
    await settle();
    expect(home()).toBeTruthy();
    // ⌘↓ from Home continues, like the pill; it lands in the empty composer.
    await key("ArrowDown");
    expect(home()).toBeNull();
    const composer = document.activeElement as HTMLTextAreaElement;
    expect(composer.tagName).toBe("TEXTAREA");
    // A composer with words in it keeps ⌘↑ for its caret.
    await act(async () => {
      const setValue = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
      setValue?.call(composer, "draft");
      composer.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await key("ArrowUp", composer);
    expect(home()).toBeNull();
    await act(async () => {
      const setValue = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
      setValue?.call(composer, "");
      composer.dispatchEvent(new Event("input", { bubbles: true }));
    });
    // Empty, ⌘↑ goes back to Home.
    await key("ArrowUp", composer);
    expect(home()).toBeTruthy();
    // Pulling up past the top of the Conversation goes back too.
    await key("ArrowDown");
    expect(home()).toBeNull();
    const transcript = page.container.querySelector('[data-testid="transcript"]') as HTMLElement;
    await act(async () => {
      transcript.dispatchEvent(new WheelEvent("wheel", { deltaY: -100, bubbles: true }));
    });
    expect(home()).toBeNull();
    await act(async () => {
      transcript.dispatchEvent(new WheelEvent("wheel", { deltaY: -100, bubbles: true }));
    });
    await settle();
    expect(home()).toBeTruthy();
  } finally {
    await page.cleanup();
  }
});

it("shows no Continue on Home while the Conversation is empty", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  engine.transcript.mockResolvedValue({
    threadId: "conv",
    messages: [],
    olderCursor: null,
    running: false,
  });
  const page = await renderShell(bootstrap());
  try {
    await settle();
    expect(page.container.querySelector('[data-testid="home"]')).toBeTruthy();
    expect(page.container.querySelector('[data-testid="continue-conversation"]')).toBeNull();
  } finally {
    await page.cleanup();
  }
});

it("lands a message link straight in the Conversation", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  engine.transcript.mockResolvedValue({
    threadId: "conv",
    messages: exchange,
    olderCursor: null,
    running: false,
  });
  const page = await renderShell(bootstrap(), "/app?m=m1");
  try {
    await settle();
    expect(page.container.querySelector('[data-testid="home"]')).toBeNull();
    expect(page.container.textContent).toContain("find the invoice");
  } finally {
    await page.cleanup();
  }
});
