// @vitest-environment jsdom
// A markdown document in the editor shell: its outline, a block picked as the ask's scope, a
// block edited in place and saved whole as a new version, and Undo saving the text before it.

import { i18n } from "@lingui/core";
import { I18nProvider } from "@lingui/react";
import type { Artifact } from "@superchat/contracts";
import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ edit: vi.fn(), listVersions: vi.fn(async () => []) }));
const chats = vi.hoisted(() => ({
  send: vi.fn(async (_input: { botId: string; text: string }) => ({ ok: true as const })),
  interrupt: vi.fn(),
  transcript: vi.fn(),
}));
vi.mock("../../lib/rpc", () => ({ rpc: { artifacts: api, chats } }));
vi.mock("../../lib/family-stream", () => ({ watchFamily: () => () => {} }));
vi.mock("@lingui/react/macro", () => ({
  useLingui: () => ({
    t: (parts: TemplateStringsArray, ...v: unknown[]) =>
      parts.reduce((out, part, i) => out + part + (v[i] ?? ""), ""),
  }),
}));

import { ArtifactEditorView } from "../artifacts/editor/ArtifactEditor";
import { resetEditorState } from "../artifacts/editor/editor-state";
import { resetNavigatorFrame } from "../artifacts/editor/NavigatorFrame";
import { docEditorKind } from "./DocEditor";

i18n.loadAndActivate({ locale: "en", messages: {} });

const BRIEF =
  "# Q3 plan\n\nLead.\n\n## Goals\n\nGrow revenue.\n\nHire later.\n\n### Revenue\n\nTwo regions.\n";

const doc = (version: number): Artifact => ({
  id: `v${version}`,
  name: "q3-plan-brief.md",
  mimeType: "text/markdown",
  botId: "bot-1",
  runId: null,
  description: null,
  size: 0,
  version,
  createdAt: "",
});

/** The engine: every saved source becomes the next version. */
const versions = new Map<string, string>();
let mounted: Root | null = null;

function Harness() {
  const [shown, setShown] = useState({ artifact: doc(1), text: BRIEF });
  return (
    <ArtifactEditorView
      artifact={shown.artifact}
      bytes={new TextEncoder().encode(shown.text)}
      kind={docEditorKind}
      onEdited={(id) =>
        setShown({ artifact: doc(Number(id.slice(1))), text: versions.get(id) ?? "" })
      }
    />
  );
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  versions.clear();
  api.edit.mockImplementation(
    async (input: { baseVersion: number; patches: Array<{ source: string }> }) => {
      const version = input.baseVersion + 1;
      versions.set(`v${version}`, input.patches[0]?.source ?? "");
      return { id: `v${version}`, version };
    },
  );
});
afterEach(async () => {
  if (mounted) await act(async () => mounted?.unmount());
  mounted = null;
  api.edit.mockReset();
  chats.send.mockClear();
  resetEditorState();
  resetNavigatorFrame();
  document.body.innerHTML = "";
});

async function mount() {
  const container = document.createElement("div");
  document.body.append(container);
  mounted = createRoot(container);
  await act(async () =>
    mounted?.render(
      <I18nProvider i18n={i18n}>
        <Harness />
      </I18nProvider>,
    ),
  );
  return container;
}

const blocks = () => [...document.querySelectorAll<HTMLElement>('[data-testid="doc-block"]')];
const chips = () =>
  [...document.querySelectorAll('[data-testid="ask-chip"]')].map((chip) => chip.textContent);
// The editor opens selectable (no Edit step, ADR 0030).
const edit = () => act(async () => {});

it("outlines the headings, nested by level, and reads every block on the page", async () => {
  await mount();
  const items = [...document.querySelectorAll<HTMLElement>('[data-testid="doc-outline-item"]')];
  expect(items.map((item) => item.textContent)).toEqual(["Q3 plan", "Goals", "Revenue"]);
  expect(items.map((item) => item.className.match(/ps-\d/)?.[0] ?? "")).toEqual([
    "",
    "ps-5",
    "ps-8",
  ]);
  expect(blocks()).toHaveLength(7);
  expect(document.querySelector('[data-testid="doc-stage"] h2')?.textContent).toBe("Goals");
});

it("picks a block: an outline, its name, and the ask is about that block", async () => {
  await mount();
  await edit();
  await act(async () => blocks()[4]?.click());
  expect(blocks()[4]?.dataset.selected).toBe("true");
  expect(blocks()[4]?.textContent).toContain("Paragraph 2");
  expect(document.querySelector('[data-testid="doc-inspector"]')?.textContent).toContain(
    "Selection · Paragraph",
  );
  expect(chips()).toEqual(["Goals · paragraph 2"]);

  const box = document.querySelector<HTMLTextAreaElement>('textarea[aria-label="Ask superchat"]');
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    setter?.call(box, "firmer");
    box?.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="Send"]')?.click());
  await act(async () => {});
  const sent = chats.send.mock.calls[0]?.[0].text ?? "";
  expect(sent).toContain('chip="Goals · paragraph 2"');
  expect(sent).toContain('<block lines="9-9">\nHire later.\n</block>');
  expect(sent.endsWith("firmer")).toBe(true);
});

it("edits a block in place and saves the whole text; Undo saves the text before it", async () => {
  await mount();
  await edit();
  await act(async () => {
    blocks()[4]?.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
  });
  const field = blocks()[4]?.querySelector("textarea") as HTMLTextAreaElement;
  expect(field.value).toBe("Hire later.");
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    setter?.call(field, "Hire in **October**.");
    field.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => {
    field.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Enter", metaKey: true, bubbles: true }),
    );
  });
  await act(async () => {});
  expect(api.edit).toHaveBeenCalledWith({
    artifactId: "v1",
    baseVersion: 1,
    patches: [
      {
        kind: "set-full-source",
        source: BRIEF.replace("Hire later.", "Hire in **October**."),
        summary: 'edited paragraph 2 under "Goals"',
      },
    ],
  });
  expect(blocks()[4]?.querySelector("strong")?.textContent).toBe("October");

  const undo = document.querySelector<HTMLButtonElement>('[aria-label="Undo"]');
  expect(undo?.disabled).toBe(false);
  await act(async () => undo?.click());
  await act(async () => {});
  expect(api.edit).toHaveBeenLastCalledWith({
    artifactId: "v2",
    baseVersion: 2,
    patches: [{ kind: "set-full-source", source: BRIEF, summary: undefined }],
  });
  expect(blocks()[4]?.textContent).toBe("Hire later.");
});

it("Escape leaves the block as it was", async () => {
  await mount();
  await edit();
  await act(async () => {
    blocks()[2]?.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
  });
  const field = blocks()[2]?.querySelector("textarea") as HTMLTextAreaElement;
  await act(async () => {
    field.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  });
  expect(api.edit).not.toHaveBeenCalled();
  expect(blocks()[2]?.querySelector("textarea")).toBeNull();
});

it("an outline entry selects its heading while editing", async () => {
  await mount();
  await edit();
  const revenue = [
    ...document.querySelectorAll<HTMLElement>('[data-testid="doc-outline-item"]'),
  ].at(-1);
  await act(async () => revenue?.click());
  expect(blocks()[5]?.dataset.selected).toBe("true");
  expect(revenue?.getAttribute("aria-current")).toBe("true");
  expect(chips()).toEqual(["Revenue · heading"]);
});
