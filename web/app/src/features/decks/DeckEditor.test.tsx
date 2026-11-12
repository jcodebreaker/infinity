// @vitest-environment jsdom

import { i18n } from "@lingui/core";
import { I18nProvider } from "@lingui/react";
import type { DeckEditTarget } from "@superchat/contracts";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

vi.mock("@lingui/react/macro", () => ({
  useLingui: () => ({
    t: (parts: TemplateStringsArray, ...v: unknown[]) =>
      parts.reduce((out, part, i) => out + part + (v[i] ?? ""), ""),
  }),
}));

const chats = vi.hoisted(() => ({
  send: vi.fn(async (_input: { botId: string; text: string }) => ({ ok: true as const })),
  interrupt: vi.fn(async (_input: { botId: string }) => ({ delivered: true })),
  transcript: vi.fn(async (_input: { botId: string }) => ({ messages: [] as unknown[] })),
}));
const artifactsApi = vi.hoisted(() => ({
  listVersions: vi.fn(async (_input: { familyId: string }) => [] as Array<{ version: number }>),
}));
const family = vi.hoisted(() => ({ listeners: [] as Array<(event: unknown) => void> }));
vi.mock("../../lib/rpc", () => ({ rpc: { chats, artifacts: artifactsApi } }));
vi.mock("../../lib/family-stream", () => ({
  watchFamily: (_botId: string, listener: (event: unknown) => void) => {
    family.listeners.push(listener);
    return () => {
      family.listeners = family.listeners.filter((l) => l !== listener);
    };
  },
}));

import { COMMIT_DEBOUNCE_MS } from "../artifacts/editor/html/committer";
import { splitElementAsk } from "../artifacts/editor/html-ask";
import { DeckViewer } from "./DeckViewer";
import { FIXTURE_DECK } from "./deck-fixture";
import { resetDeckUi } from "./deck-ui-state";

i18n.loadAndActivate({ locale: "en", messages: {} });

const NAME = "launch.deck.html";
const THEME = {
  colors: [
    { name: "--accent", value: "#1e2bfa" },
    { name: "--fg", value: "#111111" },
  ],
  fonts: [{ name: "--font-display", value: '"Space Grotesk", sans-serif', label: "Space Grotesk" }],
  slideCount: 3,
};

const title: DeckEditTarget = {
  id: "title",
  kind: "text",
  tag: "h1",
  slide: 1,
  text: "Launch",
  editable: true,
  rect: { x: 100, y: 120, w: 800, h: 90 },
  inline: {},
  computed: {
    "font-family": '"Inter", sans-serif',
    "font-size": "64px",
    "font-weight": "700",
    "font-style": "normal",
    color: "rgb(17, 17, 17)",
    "text-align": "left",
    "line-height": "76.8px",
    "letter-spacing": "0px",
    "background-color": "rgba(0, 0, 0, 0)",
    "border-radius": "0px",
    "padding-top": "0px",
    opacity: "1",
  },
  href: null,
  alt: null,
};

let root: Root;
let host: HTMLElement;
const edits: Array<{ artifactId: string; baseVersion: number; patches: unknown[] }> = [];

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.useFakeTimers();
  edits.length = 0;
  chats.send.mockClear();
  chats.interrupt.mockClear();
  chats.transcript.mockClear();
  artifactsApi.listVersions.mockClear();
  family.listeners = [];
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  vi.useRealTimers();
  resetDeckUi();
  document.body.innerHTML = "";
});

const source = async (input: { artifactId: string; baseVersion: number; patches: unknown[] }) => {
  edits.push(input);
  return { id: `v${input.baseVersion + 1}`, version: input.baseVersion + 1 };
};

async function mount(opts: { version?: number; html?: string; botId?: string | null } = {}) {
  const view = (version: number, html: string) => (
    <I18nProvider i18n={i18n}>
      <DeckViewer
        html={html}
        title={NAME}
        artifact={{ id: `v${version}`, version }}
        editSource={source}
        botId={opts.botId === undefined ? "bot-1" : opts.botId}
      />
    </I18nProvider>
  );
  await act(async () => root.render(view(opts.version ?? 1, opts.html ?? FIXTURE_DECK)));
  return {
    rerender: (version: number, html: string) => act(async () => root.render(view(version, html))),
  };
}

/** The visible edit frame, its bridge nonce and the messages the host posts into it. */
function editFrame() {
  const el = host.querySelector('[data-testid="deck-edit-frame"] iframe') as HTMLIFrameElement;
  const nonce = /NONCE = \\"([0-9a-f]{32})\\"/.exec(el.getAttribute("srcdoc") ?? "")?.[1] ?? "";
  const post = vi.fn();
  Object.defineProperty(el, "contentWindow", { value: { postMessage: post }, configurable: true });
  return { el, nonce, post };
}

async function fromFrame(frame: ReturnType<typeof editFrame>, msg: Record<string, unknown>) {
  await act(async () => {
    window.dispatchEvent(
      new MessageEvent("message", {
        data: { v: 1, nonce: frame.nonce, ...msg },
        source: frame.el.contentWindow as Window,
      }),
    );
  });
}

const select = (frame: ReturnType<typeof editFrame>, targets: DeckEditTarget[] = [title]) =>
  fromFrame(frame, { type: "superchat:edit-selection", targets });
const ready = (frame: ReturnType<typeof editFrame>) =>
  fromFrame(frame, { type: "superchat:edit-ready", theme: THEME });

function typeInto(input: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement : HTMLInputElement;
  Object.getOwnPropertyDescriptor(proto.prototype, "value")?.set?.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}
const askInput = () =>
  host.querySelector('textarea[aria-label="Ask superchat"]') as HTMLTextAreaElement;
function press(el: HTMLElement, key: string, init: KeyboardEventInit = {}) {
  el.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init }));
}
const control = (label: string) =>
  host.querySelector(`[aria-label="${label}"]`) as HTMLInputElement & HTMLButtonElement;

it("only trusts messages from its frame that carry its nonce and fit the protocol", async () => {
  await mount();
  const frame = editFrame();
  await ready(frame);
  const panel = () => host.querySelector('[data-testid="deck-style-panel"]')?.textContent ?? "";

  await act(async () => {
    window.dispatchEvent(
      new MessageEvent("message", {
        data: { v: 1, nonce: frame.nonce, type: "superchat:edit-selection", targets: [title] },
        source: {} as Window,
      }),
    );
  });
  expect(panel()).not.toContain("Heading");
  await fromFrame(frame, {
    type: "superchat:edit-selection",
    nonce: "f".repeat(32),
    targets: [title],
  });
  await fromFrame(frame, { type: "superchat:edit-selection", v: 2, targets: [title] });
  await fromFrame(frame, { type: "superchat:edit-selection", targets: [{ id: 3 }] });
  expect(panel()).not.toContain("Heading");

  await select(frame);
  expect(panel()).toContain("Slide 1 · Heading");
});

it("select, then a style control previews at once and saves one version after a pause", async () => {
  await mount();
  const frame = editFrame();
  await ready(frame);
  await select(frame);

  await act(async () => typeInto(control("Font size"), "7"));
  await act(async () => typeInto(control("Font size"), "72"));
  expect(frame.post).toHaveBeenCalledWith(
    expect.objectContaining({
      type: "superchat:edit-preview",
      id: "title",
      style: { "font-size": "72px" },
      nonce: frame.nonce,
    }),
    "*",
  );
  expect(edits).toHaveLength(0); // typing does not save a version per keystroke
  await act(async () => {
    await vi.advanceTimersByTimeAsync(COMMIT_DEBOUNCE_MS + 10);
  });
  expect(edits).toEqual([
    {
      artifactId: "v1",
      baseVersion: 1,
      patches: [
        {
          kind: "set-style",
          id: "title",
          style: { "font-size": "72px" },
          before: { "font-size": "64px" },
        },
      ],
    },
  ]);
});

it("picks theme colours and fonts as tokens, never raw values", async () => {
  await mount();
  const frame = editFrame();
  await ready(frame);
  await select(frame);
  await act(async () => control("accent").click());
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10);
  });
  expect(edits[0]?.patches).toEqual([
    {
      kind: "set-style",
      id: "title",
      style: { color: "var(--accent)" },
      before: { color: "rgb(17, 17, 17)" },
    },
  ]);
});

it("commits an inline text edit as a set-text patch", async () => {
  await mount();
  const frame = editFrame();
  await ready(frame);
  await select(frame);
  await fromFrame(frame, {
    type: "superchat:edit-text-commit",
    id: "title",
    text: "Launch day",
    before: "Launch",
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10);
  });
  expect(edits[0]?.patches).toEqual([{ kind: "set-text", id: "title", text: "Launch day" }]);
});

it("undo and redo save the earlier source back as new versions", async () => {
  const view = await mount();
  const frame = editFrame();
  await ready(frame);
  await select(frame);
  await fromFrame(frame, {
    type: "superchat:edit-text-commit",
    id: "title",
    text: "B",
    before: "A",
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10);
  });
  const edited = FIXTURE_DECK.replace("Launch", "B");
  await view.rerender(2, edited); // the saved version arrives

  const undoButton = () => control("Undo");
  expect(undoButton().disabled).toBe(false);
  await act(async () => undoButton().click());
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10);
  });
  expect(edits[1]).toEqual({
    artifactId: "v2",
    baseVersion: 2,
    patches: [{ kind: "set-full-source", source: FIXTURE_DECK }],
  });
  await view.rerender(3, FIXTURE_DECK);
  expect(control("Redo").disabled).toBe(false);
  await act(async () => control("Redo").click());
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10);
  });
  expect(edits[2]?.patches).toEqual([{ kind: "set-full-source", source: edited }]);
});

it("a change by someone else ends the history instead of undoing over it", async () => {
  const view = await mount();
  const frame = editFrame();
  await ready(frame);
  await select(frame);
  await fromFrame(frame, {
    type: "superchat:edit-text-commit",
    id: "title",
    text: "B",
    before: "A",
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10);
  });
  await view.rerender(2, FIXTURE_DECK.replace("Launch", "B"));
  expect(control("Undo").disabled).toBe(false);
  await view.rerender(3, FIXTURE_DECK.replace("Launch", "C")); // Superchat saved a version
  expect(control("Undo").disabled).toBe(true);
});

it("a patch the engine won't apply exactly says so and offers superchat", async () => {
  const refuse = async () => {
    throw Object.assign(new Error("Can't apply that edit exactly; ask Superchat instead"), {
      code: "CONFLICT",
    });
  };
  await act(async () =>
    root.render(
      <I18nProvider i18n={i18n}>
        <DeckViewer
          html={FIXTURE_DECK}
          title={NAME}
          artifact={{ id: "v1", version: 1 }}
          editSource={refuse}
          botId="bot-1"
        />
      </I18nProvider>,
    ),
  );
  const frame = editFrame();
  await ready(frame);
  await select(frame);
  await fromFrame(frame, {
    type: "superchat:edit-text-commit",
    id: "title",
    text: "B",
    before: "A",
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10);
  });
  const notice = host.querySelector('[data-testid="deck-edit-notice"]');
  expect(notice?.textContent).toContain("can't be made exactly by hand");
  const ask = [...(notice?.querySelectorAll("button") ?? [])].find((b) =>
    b.textContent?.includes("Ask superchat instead"),
  );
  await act(async () => ask?.click());
  expect(document.activeElement).toBe(askInput());
});

it("a new version keeps the selection by selecting the same ids in the new frame", async () => {
  const view = await mount();
  const frame = editFrame();
  await ready(frame);
  await select(frame);
  await view.rerender(2, FIXTURE_DECK.replace("Launch", "B"));
  const next = host.querySelector(
    '[data-testid="deck-edit-frame-next"] iframe',
  ) as HTMLIFrameElement;
  expect(next).not.toBeNull(); // loads hidden; the old frame stays until the new one is ready
  const nextFrame = {
    el: next,
    nonce: /NONCE = \\"([0-9a-f]{32})\\"/.exec(next.getAttribute("srcdoc") ?? "")?.[1] ?? "",
    post: vi.fn(),
  };
  Object.defineProperty(next, "contentWindow", {
    value: { postMessage: nextFrame.post },
    configurable: true,
  });
  await ready(nextFrame);
  expect(nextFrame.post).toHaveBeenCalledWith(
    expect.objectContaining({ type: "superchat:edit-select", ids: ["title"] }),
    "*",
  );
  expect(host.querySelector('[data-testid="deck-edit-frame-next"]')).toBeNull();
});

it("sends the hard-scope element request and the person's words to the Muse's Conversation", async () => {
  await mount();
  const frame = editFrame();
  await ready(frame);
  await select(frame);
  const chips = [...host.querySelectorAll('[data-testid="ask-chip"]')].map((c) => c.textContent);
  expect(chips).toEqual(["h1 · title"]);
  expect(askInput().placeholder).toBe("Ask superchat to change this…");
  await act(async () => typeInto(askInput(), "make it punchier"));
  await act(async () => press(askInput(), "Enter"));
  expect(chats.send).toHaveBeenCalledTimes(1);
  const sent = chats.send.mock.calls[0]?.[0];
  expect(sent?.botId).toBe("bot-1");
  expect(sent?.text).toContain('<superchat-element-request file="launch.deck.html" artifact="v1"');
  expect(sent?.text).toContain("Hard scope: change ONLY these elements");
  expect(sent?.text).toContain('<element id="title" slide="1"');
  expect(sent?.text).toContain('text: "Launch"');
  // The transcript shows it back as a chip and the person's words.
  expect(splitElementAsk(sent?.text ?? "")).toEqual({
    label: "Slide 1 · title",
    rest: "make it punchier",
    slide: 1,
  });
  expect(askInput().value).toBe("");
});

it("Shift+Enter is a new line, not a send", async () => {
  await mount();
  await act(async () => typeInto(askInput(), "first line"));
  await act(async () => press(askInput(), "Enter", { shiftKey: true }));
  expect(chats.send).not.toHaveBeenCalled();
});

it("with nothing selected it asks about the slide on screen", async () => {
  await mount();
  expect(host.querySelector('[data-testid="ask-chip"]')).toBeNull();
  expect(askInput().placeholder).toBe("Ask superchat to change this slide…");
  await act(async () => typeInto(askInput(), "warmer colours"));
  await act(async () => press(askInput(), "Enter"));
  const text = chats.send.mock.calls[0]?.[0]?.text ?? "";
  expect(text).toContain("Hard scope: change ONLY slide 1 of this deck");
  expect(splitElementAsk(text)).toEqual({ label: "Slide 1", rest: "warmer colours", slide: 1 });
});

it("shows editing, then Updated when superchat's version lands, and Undo steps back over it", async () => {
  const view = await mount();
  const frame = editFrame();
  await ready(frame);
  await select(frame);
  await act(async () => typeInto(askInput(), "shorter"));
  await act(async () => press(askInput(), "Enter"));
  const status = () => host.querySelector('[data-testid="ask-status"]')?.textContent ?? "";
  expect(status()).toContain("superchat is editing…");
  expect(host.querySelector('[data-testid="ask-box"]')?.textContent).toContain(
    "You asked: shorter",
  );
  // Stop is there while it works.
  expect(control("Stop")).not.toBeNull();

  await view.rerender(2, FIXTURE_DECK.replace("Launch", "Go")); // superchat saved a version
  expect(status()).toContain("Updated · version 2");
  expect(control("Undo").disabled).toBe(false);
  const undo = [...host.querySelectorAll('[data-testid="ask-status"] button')].find(
    (b) => b.textContent === "Undo",
  ) as HTMLButtonElement;
  await act(async () => undo.click());
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10);
  });
  expect(edits.at(-1)?.patches).toEqual([{ kind: "set-full-source", source: FIXTURE_DECK }]);
  expect(edits.at(-1)?.baseVersion).toBe(2);
});

it("a turn that ends without a new version shows superchat's reply", async () => {
  await mount();
  artifactsApi.listVersions.mockResolvedValueOnce([{ version: 1 }]);
  chats.transcript.mockResolvedValueOnce({
    messages: [
      { role: "user", blocks: [{ kind: "text", text: "ask" }] },
      { role: "bot", blocks: [{ kind: "text", text: "That title is already short." }] },
    ],
  });
  await act(async () => typeInto(askInput(), "shorter"));
  await act(async () => press(askInput(), "Enter"));
  await act(async () => {
    for (const listener of family.listeners)
      listener({ type: "turnDone", chatId: "bot-1", status: "completed" });
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(6000);
  });
  expect(host.querySelector('[data-testid="ask-status"]')?.textContent).toBe(
    "That title is already short.",
  );
});

it("an ask answered without a version does not adopt a later unrelated version into undo", async () => {
  const view = await mount();
  artifactsApi.listVersions.mockResolvedValueOnce([{ version: 1 }]);
  chats.transcript.mockResolvedValueOnce({
    messages: [{ role: "bot", blocks: [{ kind: "text", text: "Nothing to change." }] }],
  });
  await act(async () => typeInto(askInput(), "shorter"));
  await act(async () => press(askInput(), "Enter"));
  await act(async () => {
    for (const listener of family.listeners)
      listener({ type: "turnDone", chatId: "bot-1", status: "completed" });
  });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(6000);
  });
  expect(host.querySelector('[data-testid="ask-status"]')?.textContent).toBe("Nothing to change.");

  await view.rerender(2, FIXTURE_DECK.replace("Launch", "Other")); // someone else's version
  expect(control("Undo").disabled).toBe(true);
  // The reply no longer describes what is on screen.
  expect(host.querySelector('[data-testid="ask-status"]')).toBeNull();
});

it("Stop interrupts the Muse", async () => {
  await mount();
  await act(async () => typeInto(askInput(), "shorter"));
  await act(async () => press(askInput(), "Enter"));
  const stop = [...host.querySelectorAll('[data-testid="ask-status"] button')].find(
    (b) => b.textContent === "Stop",
  ) as HTMLButtonElement;
  await act(async () => stop.click());
  expect(chats.interrupt).toHaveBeenCalledWith({ botId: "bot-1" });
  expect(host.querySelector('[data-testid="ask-status"]')).toBeNull();
});

it("without a Muse to ask there is no ask box", async () => {
  await mount({ botId: null });
  expect(host.querySelector('[data-testid="ask-box"]')).toBeNull();
});
