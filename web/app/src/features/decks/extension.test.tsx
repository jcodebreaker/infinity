// @vitest-environment jsdom

import { i18n } from "@lingui/core";
import { I18nProvider } from "@lingui/react";
import type { Artifact } from "@superchat/contracts";
import { act, type ComponentProps, cloneElement, type ReactElement, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ export: vi.fn(), getById: vi.fn() }));
const download = vi.hoisted(() => vi.fn());
vi.mock("../../lib/rpc", () => ({ rpc: { decks: api, artifacts: api } }));
vi.mock("../../lib/artifact-open", () => ({
  decodeArtifactBase64: () => new Uint8Array([1]),
  downloadArtifactBytes: download,
}));
vi.mock("@lingui/react/macro", () => ({
  useLingui: () => ({
    t: (parts: TemplateStringsArray, ...v: unknown[]) =>
      parts.reduce((out, part, i) => out + part + (v[i] ?? ""), ""),
  }),
}));
vi.mock("@lingui/core/macro", () => ({
  t: (parts: TemplateStringsArray, ...v: unknown[]) =>
    parts.reduce((out, part, i) => out + part + (v[i] ?? ""), ""),
}));

vi.mock("@superchat/ui-web", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@superchat/ui-web")>();
  const Passthrough = ({ children }: { children?: ReactNode }) => <div>{children}</div>;
  return {
    ...actual,
    DropdownMenu: Passthrough,
    DropdownMenuContent: Passthrough,
    DropdownMenuItem: ({
      children,
      onClick,
      disabled,
      render,
    }: ComponentProps<"button"> & { render?: ReactElement }) =>
      render ? (
        cloneElement(render, undefined, children)
      ) : (
        <button type="button" role="menuitem" disabled={disabled} onClick={onClick}>
          {children}
        </button>
      ),
    DropdownMenuTrigger: ({ render, children }: { render?: ReactElement; children?: ReactNode }) =>
      render ? (
        cloneElement(render, undefined, children)
      ) : (
        <button type="button">{children}</button>
      ),
  };
});

import type { ArtifactEditorKind } from "../artifacts";
import { artifactKey } from "../artifacts/editor/editor-state";
import { FIXTURE_DECK } from "./deck-fixture";
import { requestDeckPresent, resetDeckUi } from "./deck-ui-state";
import { decksExtension } from "./extension";

i18n.loadAndActivate({ locale: "en", messages: {} });

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});
afterEach(() => {
  for (const fn of Object.values(api)) fn.mockReset();
  download.mockReset();
  resetDeckUi();
  document.body.innerHTML = "";
});

const file = (name: string, mimeType = "text/html"): Artifact =>
  ({ id: `id-${name}`, name, mimeType }) as Artifact;
const deck = file("launch.deck.html");

const SOURCE = new TextEncoder().encode("<html></html>");

/** A deck's header actions, as the editor shell gets them from the deck kind. */
function KindActions({ kind, artifact }: { kind: ArtifactEditorKind; artifact: Artifact }) {
  const useEditor = kind.useEditor;
  const parts = useEditor({
    artifact,
    bytes: SOURCE,
    editing: false,
    setEditing: () => {},
    focusAsk: () => {},
    closeInspector: () => {},
    locked: false,
  });
  return (
    <div data-testid="actions">
      {parts.actions}
      <span data-testid="owns-download">{String(!!parts.ownsDownload)}</span>
    </div>
  );
}

function Panel({ artifact }: { artifact: Artifact }) {
  const parts = decksExtension.usePanel({ artifact, bytes: SOURCE });
  const kind = decksExtension.editor?.(artifact) ?? null;
  return (
    <div>
      {kind ? <KindActions kind={kind} artifact={artifact} /> : null}
      <div data-testid="below">{parts.below}</div>
      <span data-testid="wide">{String(!!parts.wide)}</span>
    </div>
  );
}

async function mount(artifact: Artifact) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await act(async () =>
    root.render(
      <I18nProvider i18n={i18n}>
        <Panel artifact={artifact} />
      </I18nProvider>,
    ),
  );
  return container;
}
const buttons = (c: HTMLElement) => Array.from(c.querySelectorAll("button"));
const item = (c: HTMLElement, text: string) =>
  buttons(c).find((b) => b.textContent === text) as HTMLButtonElement;
const downloads = (c: HTMLElement) => c.querySelectorAll('[data-testid="deck-download"]');

it("views and badges only decks", () => {
  const bytes = new TextEncoder().encode(FIXTURE_DECK);
  const fallback = null;
  expect(decksExtension.view?.({ artifact: file("page.html"), bytes, fallback })).toBeNull();
  expect(decksExtension.view?.({ artifact: deck, bytes, fallback })).not.toBeNull();
  expect(decksExtension.card(file("page.html"))).toBeNull();
  expect(decksExtension.card(deck)).toBeNull();
});

it("draws a cover for HTML decks and PowerPoint files only", () => {
  const args = (name: string, mimeType: string) => ({ id: "a1", name, mimeType });
  expect(decksExtension.thumbnail?.(args("page.html", "text/html"))).toBeNull();
  expect(decksExtension.thumbnail?.(args(deck.name, deck.mimeType))).not.toBeNull();
  expect(
    decksExtension.thumbnail?.(
      args(
        "plan.pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      ),
    ),
  ).not.toBeNull();
});

it("opens decks in the editor as the deck kind, and asks for a wide panel", async () => {
  expect(decksExtension.editor?.(deck)?.id).toBe("deck");
  expect(decksExtension.editor?.(file("page.html"))).toBeNull();
  const c = await mount(deck);
  expect(c.querySelector('[data-testid="wide"]')?.textContent).toBe("true");
});

it("leaves non-deck headers alone", async () => {
  const c = await mount(file("page.html"));
  expect(buttons(c)).toEqual([]);
  expect(c.querySelector('[data-testid="wide"]')?.textContent).toBe("false");
});

it("has Theme, Present and exactly one Download menu with the three formats", async () => {
  const c = await mount(deck);
  expect(c.querySelector('[data-testid="owns-download"]')?.textContent).toBe("true");
  expect(downloads(c)).toHaveLength(1);
  expect(buttons(c).map((b) => b.textContent)).toEqual([
    "Theme",
    "Present",
    "Download",
    "PowerPoint (.pptx)",
    "PDF",
    "HTML (source)",
  ]);
  expect(c.textContent).not.toContain("Export to");
});

it("Present asks the viewer to present", async () => {
  const c = await mount(deck);
  const seen: number[] = [];
  const { useDeckPresentRequest } = await import("./deck-ui-state");
  function Spy() {
    seen.push(useDeckPresentRequest(artifactKey(deck)));
    return null;
  }
  const holder = document.createElement("div");
  await act(async () => createRoot(holder).render(<Spy />));
  await act(async () => item(c, "Present").click());
  expect(seen.at(-1)).toBe(1);
  requestDeckPresent("other.deck.html");
});

it("downloads the HTML source straight away", async () => {
  const c = await mount(deck);
  await act(async () => item(c, "HTML (source)").click());
  expect(download).toHaveBeenCalledWith("launch.deck.html", "text/html", SOURCE);
  expect(api.export).not.toHaveBeenCalled();
});

it("exports PowerPoint, shows progress on the button, then downloads with no ready bar", async () => {
  let finish: (value: Artifact) => void = () => {};
  api.export.mockReturnValue(new Promise<Artifact>((resolve) => (finish = resolve)));
  api.getById.mockResolvedValue({ name: "launch.pptx", mimeType: "x", contentBase64: "AQ==" });
  const c = await mount(deck);
  await act(async () => item(c, "PowerPoint (.pptx)").click());
  expect(api.export).toHaveBeenCalledTimes(1);
  expect(api.export.mock.calls[0]?.[0]).toEqual({
    artifactId: "id-launch.deck.html",
    format: "pptx",
  });
  expect(downloads(c)).toHaveLength(1);
  expect(c.querySelector('[data-testid="deck-download"]')?.textContent).toBe(
    "Preparing PowerPoint…",
  );
  expect(download).not.toHaveBeenCalled();

  await act(async () => finish(file("launch.pptx", "application/x")));
  expect(api.getById).toHaveBeenCalledWith({ artifactId: "id-launch.pptx" });
  expect(download).toHaveBeenCalledWith("launch.pptx", "x", expect.any(Uint8Array));
  expect(c.querySelector('[data-testid="deck-export-status"]')).toBeNull();
  expect(c.textContent).not.toContain("is ready");
  expect(c.textContent).toContain("Added to your Library");
});

it("sends pdf for the PDF pick and says calmly when it fails, with a retry", async () => {
  api.export.mockRejectedValueOnce(new Error("engine exploded: stack trace"));
  const c = await mount(deck);
  await act(async () => item(c, "PDF").click());
  expect(api.export.mock.calls[0]?.[0]).toEqual({
    artifactId: "id-launch.deck.html",
    format: "pdf",
  });
  const text = c.querySelector('[data-testid="deck-download"]')?.textContent ?? "";
  expect(text).toContain("Couldn't prepare PDF");
  expect(text).not.toContain("stack trace");
  expect(download).not.toHaveBeenCalled();
  api.export.mockResolvedValueOnce(file("launch.pdf", "application/pdf"));
  api.getById.mockResolvedValue({ name: "launch.pdf", mimeType: "p", contentBase64: "AQ==" });
  await act(async () =>
    (c.querySelector('[data-testid="deck-download"]') as HTMLButtonElement).click(),
  );
  expect(api.export).toHaveBeenCalledTimes(2);
  expect(download).toHaveBeenCalledWith("launch.pdf", "p", expect.any(Uint8Array));
});

it("can cancel a running export and never downloads its result", async () => {
  let finish: (value: Artifact) => void = () => {};
  api.export.mockReturnValue(new Promise<Artifact>((resolve) => (finish = resolve)));
  api.getById.mockResolvedValue({ name: "launch.pptx", mimeType: "x", contentBase64: "AQ==" });
  const c = await mount(deck);
  await act(async () => item(c, "PowerPoint (.pptx)").click());
  await act(async () =>
    (c.querySelector('button[aria-label="Cancel"]') as HTMLButtonElement).click(),
  );
  expect(c.querySelector('[data-testid="deck-download"]')?.textContent).toBe("Download");
  await act(async () => finish(file("launch.pptx", "application/x")));
  expect(download).not.toHaveBeenCalled();
});

const PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation";

it("previews PowerPoint files and only widens their panel", async () => {
  const bytes = new Uint8Array([80, 75]);
  const fallback = null;
  const pptx = file("review.pptx", PPTX);
  expect(decksExtension.view?.({ artifact: pptx, bytes, fallback })).not.toBeNull();
  expect(
    decksExtension.view?.({
      artifact: file("review.pptx", "application/octet-stream"),
      bytes,
      fallback,
    }),
  ).not.toBeNull();
  expect(
    decksExtension.view?.({ artifact: file("notes.txt", "text/plain"), bytes, fallback }),
  ).toBeNull();
  expect(decksExtension.card(pptx)).toBeNull();
  expect(decksExtension.editor?.(pptx)).toBeNull();
  const c = await mount(pptx);
  expect(buttons(c)).toEqual([]);
  expect(c.querySelector('[data-testid="wide"]')?.textContent).toBe("true");
});
