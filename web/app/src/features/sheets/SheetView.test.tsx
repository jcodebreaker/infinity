// @vitest-environment jsdom

import { i18n } from "@lingui/core";
import { I18nProvider } from "@lingui/react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  table: vi.fn(),
  editTable: vi.fn(),
  tableRange: vi.fn(),
  listVersions: vi.fn(),
}));
const chats = vi.hoisted(() => ({
  send: vi.fn(async (_input: { botId: string; text: string }) => ({ ok: true as const })),
  interrupt: vi.fn(),
  transcript: vi.fn(),
}));
vi.mock("../../lib/rpc", () => ({ rpc: { artifacts: api, sheets: api, chats } }));
vi.mock("../../lib/family-stream", () => ({ watchFamily: () => () => {} }));
vi.mock("@lingui/react/macro", () => ({
  useLingui: () => ({
    t: (parts: TemplateStringsArray, ...v: unknown[]) =>
      parts.reduce((out, part, i) => out + part + (v[i] ?? ""), ""),
  }),
}));

import {
  artifactKey,
  focusArtifactPart,
  resetEditorState,
  setArtifactMarked,
} from "../artifacts/editor/editor-state";
import { resetNavigatorFrame } from "../artifacts/editor/NavigatorFrame";
import { SheetView } from "./SheetView";

i18n.loadAndActivate({ locale: "en", messages: {} });

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});
afterEach(() => {
  for (const fn of Object.values(api)) fn.mockReset();
  chats.send.mockClear();
  resetEditorState();
  resetNavigatorFrame();
  document.body.innerHTML = "";
});

const cell = (v: string | number | null, f: string | null = null) => ({
  v,
  f,
  t: v === null ? null : typeof v === "number" ? "n" : "s",
});
const tableOf = (id: string, version: number, amount = 64000) => ({
  artifactId: id,
  version,
  kind: "xlsx",
  sheets: [
    {
      name: "Sales",
      frozenRows: 1,
      nRows: 4,
      nCols: 3,
      truncated: false,
      rows: [
        [cell("Region"), cell("Account"), cell("Revenue")],
        [cell("Ontario"), cell("Northwind"), cell(amount)],
        [cell("Quebec"), cell("Acme"), cell(48000)],
        [cell("Total"), cell(null), cell(null, "=SUM(C2:C3)")],
      ],
    },
  ],
});
const ver = (id: string, version: number, extra = {}) => ({
  id,
  version,
  name: "q3.xlsx",
  createdAt: "",
  origin: "ai",
  parentVersionId: null,
  ...extra,
});

const tick = () => act(async () => {});
let root: Root;
async function render(node: React.ReactNode) {
  await act(async () => root.render(<I18nProvider i18n={i18n}>{node}</I18nProvider>));
  await tick();
}
async function mount(node: React.ReactNode) {
  const container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await render(node);
  return container;
}
const at = (container: HTMLElement, r: number, c: number) =>
  container.querySelector<HTMLElement>(`td[data-r="${r}"][data-c="${c}"]`) as HTMLElement;
const button = (container: HTMLElement, name: RegExp) =>
  [...container.querySelectorAll("button")].find((b) =>
    name.test(b.getAttribute("aria-label") ?? b.textContent ?? ""),
  ) as HTMLButtonElement;
const mouse = (el: Element, type: string, init: MouseEventInit = {}) =>
  act(async () => {
    el.dispatchEvent(new MouseEvent(type, { bubbles: true, ...init }));
  });
const key = (el: Element, k: string, init: KeyboardEventInit = {}) =>
  act(async () => {
    el.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, ...init }));
  });
const edit = (container: HTMLElement) =>
  act(async () =>
    (container.querySelector('[data-testid="editor-edit"]') as HTMLButtonElement).click(),
  );
async function ask(container: HTMLElement, words: string) {
  const box = container.querySelector(
    'textarea[aria-label="Ask superchat"]',
  ) as HTMLTextAreaElement;
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    setter?.call(box, words);
    box.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => button(container, /^Send$/).click());
  await tick();
}
const grid = (container: HTMLElement) => container.querySelector('[role="grid"]') as HTMLElement;
async function type(input: HTMLInputElement, value: string) {
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

const artifact = { id: "a2", name: "q3.xlsx", version: 2 };

it("renders the grid: frozen header, right-aligned numbers, computed and fallback formulas", async () => {
  const withUnsupported = tableOf("a2", 2);
  withUnsupported.sheets[0]!.rows[3]![1] = cell(null, "=VLOOKUP(1,A1:B2,2,FALSE)");
  api.table.mockResolvedValue(withUnsupported);
  api.listVersions.mockResolvedValue([ver("a2", 2)]);
  const view = await mount(<SheetView artifact={artifact} />);

  expect(view.querySelectorAll("thead th")[1]?.textContent).toBe("A");
  const header = at(view, 0, 0);
  expect(header.textContent).toBe("Region");
  expect(header.className).toContain("sticky");
  expect(at(view, 1, 0).className).not.toContain("sticky");

  expect(at(view, 1, 2).textContent).toBe("64000");
  expect(at(view, 1, 2).className).toContain("text-right");
  expect(at(view, 1, 0).className).toContain("text-left");

  // A formula with no cached value is worked out in the browser.
  expect(at(view, 3, 2).textContent).toBe("112000");
  expect(at(view, 3, 2).className).toContain("text-right");
  // One it cannot work out shows its text, muted.
  const unsupported = at(view, 3, 1);
  expect(unsupported.textContent).toBe("=VLOOKUP(1,A1:B2,2,FALSE)");
  expect(unsupported.className).toContain("text-muted-foreground");
});

it("marks cells the person changed by hand against the last version Superchat saved", async () => {
  api.table.mockImplementation(async ({ artifactId }: { artifactId: string }) =>
    artifactId === "a1" ? tableOf("a1", 1, 64000) : tableOf("a2", 2, 70000),
  );
  api.listVersions.mockResolvedValue([
    ver("a2", 2, { origin: "manual", parentVersionId: "a1" }),
    ver("a1", 1),
  ]);
  const view = await mount(<SheetView artifact={artifact} />);

  expect(view.querySelectorAll('[role="img"][aria-label="Edited by you"]').length).toBe(1);
  expect(at(view, 1, 2).querySelector('[aria-label="Edited by you"]')).not.toBeNull();
  expect(at(view, 1, 0).querySelector('[aria-label="Edited by you"]')).toBeNull();
  expect(view.querySelector('[data-testid="sheet-changes"]')?.textContent).toContain(
    "You changed 1 cell on Sales",
  );
});

it("saves an edit as a new version and switches to it", async () => {
  api.table.mockImplementation(async ({ artifactId }: { artifactId: string }) =>
    artifactId === "a3" ? tableOf("a3", 3, 70000) : tableOf("a2", 2),
  );
  api.listVersions.mockResolvedValue([ver("a2", 2)]);
  api.editTable.mockResolvedValue({ id: "a3", version: 3 });
  const onEdited = vi.fn();
  const view = await mount(<SheetView artifact={artifact} onEdited={onEdited} />);

  // Read-only until Edit is on: typing does nothing.
  await mouse(at(view, 1, 2), "mousedown");
  await key(grid(view), "7");
  expect(view.querySelector("input")).toBeNull();

  await edit(view);
  await key(grid(view), "7");
  const input = view.querySelector("input") as HTMLInputElement;
  expect(input.value).toBe("7");
  await type(input, "70000");
  await key(input, "Enter");
  await tick();

  expect(api.editTable).toHaveBeenCalledWith({
    artifactId: "a2",
    baseVersion: 2,
    edits: [{ sheet: "Sales", row: 1, col: 2, value: 70000 }],
  });
  expect(onEdited).toHaveBeenCalledWith("a3");
  expect(api.table).toHaveBeenCalledWith({ artifactId: "a3" });
  expect(at(view, 1, 2).textContent).toBe("70000");
});

it("reloads and re-applies the edit once when the version is stale", async () => {
  api.table.mockImplementation(async ({ artifactId }: { artifactId: string }) =>
    artifactId === "a9"
      ? tableOf("a9", 9, 70000)
      : tableOf(artifactId, artifactId === "a8" ? 8 : 2),
  );
  // Newest at open is a2; by the time the edit lands, a8 has been saved.
  api.listVersions
    .mockResolvedValueOnce([ver("a2", 2)])
    .mockResolvedValue([ver("a8", 8), ver("a2", 2)]);
  api.editTable
    .mockRejectedValueOnce({ code: "CONFLICT" })
    .mockResolvedValueOnce({ id: "a9", version: 9 });
  const view = await mount(<SheetView artifact={artifact} />);
  await edit(view);
  await mouse(at(view, 1, 2), "mousedown");
  await key(grid(view), "Enter");
  await type(view.querySelector("input") as HTMLInputElement, "70000");
  await key(view.querySelector("input") as HTMLInputElement, "Enter");
  await tick();

  expect(api.editTable).toHaveBeenCalledTimes(2);
  expect(api.editTable).toHaveBeenLastCalledWith({
    artifactId: "a8",
    baseVersion: 8,
    edits: [{ sheet: "Sales", row: 1, col: 2, value: 70000 }],
  });
  expect(view.querySelector('[role="alert"]')).toBeNull();
});

it("shows a calm error and keeps the file as it was when saving fails twice", async () => {
  api.table.mockResolvedValue(tableOf("a2", 2));
  api.listVersions.mockResolvedValue([ver("a2", 2)]);
  api.editTable.mockRejectedValue(new Error("boom"));
  const view = await mount(<SheetView artifact={artifact} />);
  await edit(view);
  await mouse(at(view, 1, 2), "mousedown");
  await key(grid(view), "Enter");
  await type(view.querySelector("input") as HTMLInputElement, "1");
  await key(view.querySelector("input") as HTMLInputElement, "Enter");
  await tick();

  expect(view.querySelector('[role="alert"]')?.textContent).toBe("Couldn't save that change.");
  expect(at(view, 1, 2).textContent).toBe("64000");
});

it("moves the active cell with the arrow keys and shows its source in the formula bar", async () => {
  api.table.mockResolvedValue(tableOf("a2", 2));
  api.listVersions.mockResolvedValue([ver("a2", 2)]);
  const view = await mount(<SheetView artifact={artifact} />);
  const bar = () => view.querySelector('[data-testid="formula-bar"]')?.textContent;
  await mouse(at(view, 2, 2), "mousedown");
  expect(bar()).toBe("48000");
  await key(grid(view), "ArrowDown");
  expect(bar()).toBe("=SUM(C2:C3)");
  await key(grid(view), "ArrowLeft");
  expect(bar()).toBe("");
});

const formatted = () => {
  const table = tableOf("a2", 2);
  const sheet = table.sheets[0] as (typeof table.sheets)[0] & { colWidths?: number[] };
  const rows = sheet.rows as Array<Array<Record<string, unknown>>>;
  (rows[1] as Array<Record<string, unknown>>)[2] = { ...cell(64000), z: "$#,##0.00" };
  (rows[2] as Array<Record<string, unknown>>)[1] = {
    v: "2024-03-01T00:00:00",
    f: null,
    t: "d",
    z: "mmm d, yyyy",
  };
  sheet.colWidths = [10, 30, 12];
  return table;
};

it("shows numbers and dates through their format, but the formula bar and editor keep the raw value", async () => {
  api.table.mockResolvedValue(formatted());
  api.listVersions.mockResolvedValue([ver("a2", 2)]);
  const view = await mount(<SheetView artifact={artifact} />);

  expect(at(view, 1, 2).textContent).toBe("$64,000.00");
  expect(at(view, 2, 1).textContent).toBe("Mar 1, 2024");
  expect(at(view, 2, 1).className).toContain("text-right");

  await mouse(at(view, 1, 2), "mousedown");
  expect(view.querySelector('[data-testid="formula-bar"]')?.textContent).toBe("64000");
  await mouse(at(view, 2, 1), "mousedown");
  expect(view.querySelector('[data-testid="formula-bar"]')?.textContent).toBe(
    "2024-03-01T00:00:00",
  );

  await edit(view);
  await mouse(at(view, 1, 2), "mousedown");
  await key(grid(view), "Enter");
  expect((view.querySelector("input") as HTMLInputElement).value).toBe("64000");
});

it("sizes columns from the file's widths", async () => {
  api.table.mockResolvedValue(formatted());
  api.listVersions.mockResolvedValue([ver("a2", 2)]);
  const view = await mount(<SheetView artifact={artifact} />);
  const cols = [...view.querySelectorAll("colgroup col")].map(
    (c) => (c as HTMLElement).style.width,
  );
  expect(cols).toEqual(["48px", "108px", "260px", "124px"]);
  // A cell that may be clipped carries its full value as a tooltip.
  expect(at(view, 2, 1).title).toBe("Mar 1, 2024");
});

it("keeps the number format on an edited cell and the date type on an undone date; Redo puts it back", async () => {
  const after = formatted();
  (after.sheets[0]?.rows[2]?.[1] as { v: string }).v = "2025-01-15T00:00:00";
  api.table.mockImplementation(async ({ artifactId }: { artifactId: string }) =>
    artifactId === "a3" ? { ...after, artifactId: "a3", version: 3 } : formatted(),
  );
  api.listVersions
    .mockResolvedValueOnce([ver("a2", 2)])
    .mockResolvedValue([ver("a3", 3, { origin: "manual", parentVersionId: "a2" }), ver("a2", 2)]);
  api.editTable.mockResolvedValue({ id: "a3", version: 3 });
  const view = await mount(<SheetView artifact={artifact} />);
  await edit(view);
  await mouse(at(view, 2, 1), "mousedown");
  await key(grid(view), "Enter");
  const input = view.querySelector("input") as HTMLInputElement;
  await type(input, "2025-01-15T00:00:00");
  await key(input, "Enter");
  expect(at(view, 2, 1).textContent).toBe("Jan 15, 2025");
  expect(api.editTable.mock.calls[0]?.[0].edits).toEqual([
    { sheet: "Sales", row: 2, col: 1, value: "2025-01-15T00:00:00" },
  ]);
  await tick();
  // Undo sends the old value back as the ISO text it was read as; the engine keeps it a date.
  await act(async () => button(view, /^Undo$/).click());
  expect(api.editTable.mock.calls[1]?.[0].edits).toEqual([
    { sheet: "Sales", row: 2, col: 1, value: "2024-03-01T00:00:00" },
  ]);
  await tick();
  // The shell's toolbar holds the one undo; the changes strip has none of its own.
  expect(view.querySelector('[data-testid="sheet-changes"] button')).toBeNull();
  await act(async () => button(view, /^Redo$/).click());
  expect(api.editTable.mock.calls[2]?.[0].edits).toEqual([
    { sheet: "Sales", row: 2, col: 1, value: "2025-01-15T00:00:00" },
  ]);
});

it("lists a workbook's sheets in the navigator and switches between them; a CSV has none", async () => {
  const two = tableOf("a2", 2);
  two.sheets.push({ ...structuredClone(two.sheets[0]!), name: "By region" });
  api.table.mockResolvedValue(two);
  api.listVersions.mockResolvedValue([ver("a2", 2)]);
  const view = await mount(<SheetView artifact={artifact} />);
  const tabs = () => [...view.querySelectorAll<HTMLElement>('[data-testid="sheet-tab"]')];
  expect(tabs().map((tab) => tab.textContent)).toEqual(["Sales", "By region"]);
  expect(tabs()[0]?.getAttribute("aria-current")).toBe("true");
  await act(async () => tabs()[1]?.click());
  expect(grid(view).getAttribute("aria-label")).toBe("By region");
  expect(tabs()[1]?.getAttribute("aria-current")).toBe("true");

  api.table.mockResolvedValue({ ...tableOf("c1", 1), kind: "csv" });
  const csv = await mount(<SheetView artifact={{ id: "c1", name: "q3.csv", version: 1 }} />);
  expect(csv.querySelector('[data-testid="sheet-list"]')).toBeNull();
  expect(csv.querySelector('[data-testid="version-label"]')?.textContent).toBe("Sheet · version 1");
});

it("asks about the selected range with the engine's excerpt, else about the whole sheet", async () => {
  api.table.mockResolvedValue(tableOf("a2", 2));
  api.listVersions.mockResolvedValue([ver("a2", 2)]);
  api.tableRange.mockResolvedValue({ version: 2, block: "[selection ...]" });
  const view = await mount(<SheetView artifact={artifact} botId="bot-1" />);
  const chips = (on = view) =>
    [...on.querySelectorAll('[data-testid="ask-chip"]')].map((chip) => chip.textContent);
  const box = (on = view) =>
    on.querySelector('textarea[aria-label="Ask superchat"]') as HTMLTextAreaElement;

  expect(chips()).toEqual([]);
  expect(box().placeholder).toBe("Ask superchat to change this sheet…");
  await ask(view, "Add a total");
  expect(chats.send.mock.calls[0]?.[0].text).toContain('chip="Whole sheet"');
  expect(api.tableRange).not.toHaveBeenCalled();

  // A fresh editor: the first ask is still working.
  const other = await mount(<SheetView artifact={artifact} botId="bot-1" />);
  await mouse(at(other, 1, 1), "mousedown");
  await mouse(at(other, 2, 2), "mousedown", { shiftKey: true });
  expect(chips(other)).toEqual(["Sales · B2:C3"]);
  expect(box(other).placeholder).toBe("Ask superchat to change this…");
  await ask(other, "Round these");
  expect(api.tableRange).toHaveBeenCalledWith({ artifactId: "a2", sheet: "Sales", range: "B2:C3" });
  expect(chats.send.mock.calls.at(-1)?.[0].text).toBe("[selection ...]\n\nRound these");

  await act(async () => button(other, /^Remove Sales · B2:C3$/).click());
  expect(chips(other)).toEqual([]);
});

it("shows the selection's sum, count and average in the inspector while editing", async () => {
  api.table.mockResolvedValue(tableOf("a2", 2));
  api.listVersions.mockResolvedValue([ver("a2", 2)]);
  const view = await mount(<SheetView artifact={artifact} />);
  const inspector = () => view.querySelector('[data-testid="sheet-inspector"]');
  expect(inspector()).toBeNull();
  await edit(view);
  expect(inspector()?.textContent).toContain("Select cells");
  await mouse(at(view, 1, 1), "mousedown");
  await mouse(at(view, 2, 2), "mousedown", { shiftKey: true });
  const text = inspector()?.textContent ?? "";
  expect(text).toContain("Selection · 4 cells");
  expect(text).toContain("Sum112,000");
  expect(text).toContain("Count2");
  expect(text).toContain("Average56,000");
});

it("an older version is read-only: no Edit", async () => {
  api.table.mockResolvedValue(tableOf("a2", 2));
  api.listVersions.mockResolvedValue([ver("a3", 3), ver("a2", 2)]);
  const view = await mount(<SheetView artifact={artifact} />);
  expect(view.querySelector('[data-testid="editor-edit"]')).toBeNull();
});

it("Undo steps back over superchat's answer to an ask from here", async () => {
  api.table.mockImplementation(async ({ artifactId }: { artifactId: string }) =>
    artifactId === "a3" ? tableOf("a3", 3, 99) : tableOf("a2", 2),
  );
  api.listVersions.mockResolvedValue([ver("a2", 2)]);
  api.tableRange.mockResolvedValue({ version: 2, block: "[selection ...]" });
  api.editTable.mockResolvedValue({ id: "a4", version: 4 });
  const view = await mount(<SheetView artifact={artifact} botId="bot-1" />);
  await edit(view);
  await ask(view, "Make it 99");
  api.listVersions.mockResolvedValue([ver("a3", 3), ver("a2", 2)]);
  await render(<SheetView artifact={{ id: "a3", name: "q3.xlsx", version: 3 }} botId="bot-1" />);
  expect(at(view, 1, 2).textContent).toBe("99");

  await act(async () => button(view, /^Undo$/).click());
  expect(api.editTable).toHaveBeenCalledWith({
    artifactId: "a3",
    baseVersion: 3,
    edits: [{ sheet: "Sales", row: 1, col: 2, value: 64000 }],
  });
});

it("marks the cells a live update changed and goes to the one its chip asks for", async () => {
  const two = tableOf("a2", 2);
  two.sheets.push({ ...structuredClone(two.sheets[0]!), name: "By region" });
  api.table.mockResolvedValue(two);
  api.listVersions.mockResolvedValue([ver("a2", 2)]);
  const view = await mount(<SheetView artifact={artifact} />);
  const key = artifactKey({ botId: undefined as never, name: artifact.name });
  await act(async () => setArtifactMarked(key, ["C2", "By region!A3", "not a cell"]));
  expect(at(view, 1, 2).dataset.marked).toBe("true");
  expect(at(view, 2, 0).dataset.marked).toBeUndefined(); // that one is on the other sheet
  await act(async () => focusArtifactPart(key, "By region!A3"));
  expect(grid(view).getAttribute("aria-label")).toBe("By region");
  expect(at(view, 2, 0).dataset.marked).toBe("true");
  expect(at(view, 2, 0).className).toContain("ring-2");
});
