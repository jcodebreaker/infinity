// @vitest-environment jsdom
// An HTML page / mini app in the artifact editor: Run vs Edit, the shared edit bridge in page
// mode, layers, the inspector and ask chips for a selection, manual patches through the engine's
// edit route, and an element without an id offered to superchat instead.

import { i18n } from "@lingui/core";
import { I18nProvider } from "@lingui/react";
import type { Artifact, DeckEditTarget } from "@superchat/contracts";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

vi.mock("@lingui/react/macro", () => ({
  useLingui: () => ({
    t: (parts: TemplateStringsArray, ...v: unknown[]) =>
      parts.reduce((out, part, i) => out + part + (v[i] ?? ""), ""),
  }),
}));
const artifacts = vi.hoisted(() => ({
  listVersions: vi.fn(),
  edit: vi.fn(async (input: { baseVersion: number }) => ({
    id: `p${input.baseVersion + 1}`,
    version: input.baseVersion + 1,
  })),
}));
const chats = vi.hoisted(() => ({
  send: vi.fn(async (_input: { botId: string; text: string }) => ({ ok: true as const })),
  interrupt: vi.fn(),
  transcript: vi.fn(),
}));
vi.mock("../../lib/rpc", () => ({ rpc: { chats, artifacts } }));
vi.mock("../../lib/family-stream", () => ({ watchFamily: () => () => {} }));

import { ArtifactEditorView } from "../artifacts/editor/ArtifactEditor";
import { resetEditorState } from "../artifacts/editor/editor-state";
import { COMMIT_DEBOUNCE_MS } from "../artifacts/editor/html/committer";
import { isAppHtml, pageEditorKind, pageLayers } from "./PageEditor";

i18n.loadAndActivate({ locale: "en", messages: {} });

const PAGE = `<!doctype html><html><head><title>Expenses</title></head><body>
<header data-superchat-id="header"><h1 data-superchat-id="title">Expenses</h1></header>
<main data-superchat-id="main">
  <form data-superchat-id="add-form" aria-label="Add form"><input><button data-superchat-id="add-button">Add</button></form>
  <section data-superchat-id="history"><ul></ul></section>
</main>
<script>document.querySelector("ul").innerHTML = "<li>Coffee 4.50</li>";</script>
</body></html>`;

const NAME = "tracker.html";
const page = (version = 1): Artifact => ({
  id: `p${version}`,
  version,
  name: NAME,
  mimeType: "text/html",
  botId: "bot-1",
  runId: null,
  description: "Expense tracker",
  size: PAGE.length,
  createdAt: "",
});
const target = (over: Partial<DeckEditTarget>): DeckEditTarget => ({
  id: "add-button",
  kind: "text",
  tag: "button",
  slide: 1,
  text: "Add",
  editable: true,
  rect: { x: 0, y: 0, w: 80, h: 30 },
  inline: {},
  computed: { "font-size": "14px" },
  href: null,
  alt: null,
  where: 'main[data-superchat-id="main"] > form > button',
  ...over,
});

let root: Root;
let host: HTMLElement;
beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.useFakeTimers();
  vi.stubGlobal(
    "URL",
    Object.assign(URL, { createObjectURL: () => "blob:x", revokeObjectURL: () => {} }),
  );
  artifacts.edit.mockClear();
  chats.send.mockClear();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  vi.useRealTimers();
  vi.unstubAllGlobals();
  resetEditorState();
  document.body.innerHTML = "";
});

async function mount() {
  const bytes = new TextEncoder().encode(PAGE);
  await act(async () =>
    root.render(
      <I18nProvider i18n={i18n}>
        <ArtifactEditorView artifact={page()} bytes={bytes} kind={pageEditorKind} />
      </I18nProvider>,
    ),
  );
}
const button = (text: string) =>
  [...host.querySelectorAll<HTMLElement>("button")].find((b) => b.textContent?.trim() === text);

function editFrame() {
  const el = host.querySelector('[data-testid="deck-edit-frame"] iframe') as HTMLIFrameElement;
  const srcdoc = el.getAttribute("srcdoc") ?? "";
  const nonce = /NONCE = \\"([0-9a-f]{32})\\"/.exec(srcdoc)?.[1] ?? "";
  const post = vi.fn();
  Object.defineProperty(el, "contentWindow", { value: { postMessage: post }, configurable: true });
  return { el, srcdoc, nonce, post };
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
const ready = (frame: ReturnType<typeof editFrame>) =>
  fromFrame(frame, {
    type: "superchat:edit-ready",
    theme: { colors: [], fonts: [], slideCount: 0 },
  });

it("reads layers and tells an app from a page", () => {
  expect(pageLayers(PAGE)).toEqual([
    { id: "header", label: "Header", tag: "header", depth: 0 },
    { id: "main", label: "Main", tag: "main", depth: 0 },
    { id: "add-form", label: "Add form", tag: "form", depth: 1 },
    { id: "history", label: "History", tag: "section", depth: 1 },
  ]);
  expect(isAppHtml(PAGE)).toBe(true);
  expect(isAppHtml("<h1>Hi</h1>")).toBe(false);
});

it("runs the app live, and Edit puts the page-mode bridge on it", async () => {
  await mount();
  expect(host.querySelector('[data-testid="version-label"]')?.textContent).toBe("App · version 1");
  const run = host.querySelector('[data-testid="page-run"]') as HTMLElement;
  expect(run.getAttribute("aria-pressed")).toBe("true");
  expect(host.querySelector("iframe")?.getAttribute("srcdoc")).not.toContain(
    "superchat-edit-bridge",
  );
  expect(host.querySelectorAll('[data-testid="page-layer"]')).toHaveLength(4);

  await act(async () => button("Edit")?.click());
  const frame = editFrame();
  expect(frame.srcdoc).toContain("superchat-edit-bridge");
  expect(frame.srcdoc).toContain('var PAGE = \\"page\\" === \\"page\\"');
  expect(run.getAttribute("aria-pressed")).toBe("false");
  // Run leaves edit mode for the live app.
  await act(async () => run.click());
  expect(host.querySelector('[data-testid="deck-edit-frame"]')).toBeNull();
});

it("select the Add button: a chip, no inspector; typing a new label saves a manual patch", async () => {
  await mount();
  await act(async () => button("Edit")?.click());
  const frame = editFrame();
  await ready(frame);
  await fromFrame(frame, { type: "superchat:edit-selection", targets: [target({})] });
  // Selecting only outlines and names the element; the inspector opens on "Edit style".
  expect(host.querySelector('[data-testid="deck-style-panel"]')).toBeNull();
  const chips = [...host.querySelectorAll('[data-testid="ask-chip"]')].map((c) => c.textContent);
  expect(chips).toEqual(["button · add-button"]);

  await fromFrame(frame, {
    type: "superchat:edit-text-commit",
    id: "add-button",
    text: "Save",
    before: "Add",
  });
  await act(async () => vi.advanceTimersByTimeAsync(COMMIT_DEBOUNCE_MS));
  expect(artifacts.edit).toHaveBeenCalledWith({
    artifactId: "p1",
    baseVersion: 1,
    patches: [{ kind: "set-text", id: "add-button", text: "Save" }],
  });
});

it("an element with no id is asked about by its place, never patched by hand", async () => {
  await mount();
  await act(async () => button("Edit")?.click());
  const frame = editFrame();
  await ready(frame);
  const item = target({
    id: "~2.1.0.0",
    tag: "li",
    text: "Coffee 4.50",
    editable: false,
    where: 'main[data-superchat-id="main"] > section > ul > li',
  });
  await fromFrame(frame, { type: "superchat:edit-selection", targets: [item] });
  expect(host.querySelector('[data-testid="style-panel-unstable"]')).not.toBeNull();
  expect([...host.querySelectorAll('[data-testid="ask-chip"]')].map((c) => c.textContent)).toEqual([
    "li · Coffee 4.50",
  ]);
  // A key press in the frame (delete) is refused here, without calling the engine.
  await fromFrame(frame, { type: "superchat:edit-key", action: "delete" });
  await act(async () => vi.advanceTimersByTimeAsync(COMMIT_DEBOUNCE_MS));
  expect(artifacts.edit).not.toHaveBeenCalled();
  expect(host.querySelector('[data-testid="deck-edit-notice"]')).not.toBeNull();

  const box = host.querySelector('textarea[aria-label="Ask superchat"]') as HTMLTextAreaElement;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set?.call(
      box,
      "Show the amount in bold",
    );
    box.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => (host.querySelector('[aria-label="Send"]') as HTMLElement).click());
  const text = chats.send.mock.calls[0]?.[0].text ?? "";
  expect(text).toContain('file="tracker.html" artifact="p1" version="1"');
  expect(text).toContain("<element where=\"main[data-superchat-id='main'] › section › ul › li\"");
  expect(text).not.toContain("~2.1.0.0");
  expect(text.endsWith("\n\nShow the amount in bold")).toBe(true);
});

it("a layer selects its section through the bridge, entering edit mode first", async () => {
  await mount();
  const layer = [...host.querySelectorAll<HTMLElement>('[data-testid="page-layer"]')].find(
    (el) => el.textContent === "Add form",
  );
  await act(async () => layer?.click());
  const frame = editFrame();
  await ready(frame);
  expect(frame.post).toHaveBeenCalledWith(
    expect.objectContaining({ type: "superchat:edit-select", ids: ["add-form"] }),
    "*",
  );
});
