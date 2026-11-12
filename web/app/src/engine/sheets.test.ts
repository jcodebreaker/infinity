import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./client", () => ({
  engineClient: () => ({ baseUrl: "http://engine.test", proxySecret: "", secrets: [] }),
  museSessionId: async () => "sess-1",
  NO_EMAIL: "",
}));

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const call = (proc: unknown, input?: unknown) =>
  (proc as { callable: () => (input: never) => Promise<unknown> }).callable()(input as never);

afterEach(() => vi.unstubAllGlobals());

const { sheetsRouter } = await import("./sheets");

const row = {
  id: "a1",
  parent_session_id: "sess-1",
  name: "report.csv",
  title: "Report",
  kind: "csv",
  mime: "text/csv; charset=utf-8",
  size: 12,
  version: 2,
  versions: 2,
  published: false,
  created_at: 1_700_000_000,
  updated_at: null,
};

const table = {
  artifact_id: "a1",
  version: 3,
  kind: "csv",
  sheets: [
    {
      name: "Sheet1",
      rows: [[{ v: "Region", f: null, t: "s" }]],
      frozen_rows: 1,
      n_rows: 1,
      n_cols: 1,
      truncated: false,
    },
  ],
};

describe("sheets", () => {
  it("reads a table with camel-cased sheet fields", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: URL) => {
        expect(url.pathname).toBe("/v1/artifacts/a1/table");
        return json(table);
      }),
    );
    expect(await call(sheetsRouter.sheets.table, { artifactId: "a1" })).toMatchObject({
      artifactId: "a1",
      version: 3,
      sheets: [{ name: "Sheet1", frozenRows: 1, nRows: 1, nCols: 1, truncated: false }],
    });
  });

  it("passes number formats and column widths through", async () => {
    const withFormats = {
      ...table,
      sheets: [
        {
          ...table.sheets[0],
          rows: [[{ v: 64000, f: null, t: "n", z: "$#,##0" }]],
          col_widths: [12, null],
        },
      ],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json(withFormats)),
    );
    const result = (await call(sheetsRouter.sheets.table, { artifactId: "a1" })) as {
      sheets: { rows: { z?: string }[][]; colWidths?: (number | null)[] }[];
    };
    expect(result.sheets[0]?.rows[0]?.[0]?.z).toBe("$#,##0");
    expect(result.sheets[0]?.colWidths).toEqual([12, null]);
  });

  it("sends edits against a base version and returns the new version", async () => {
    const fetchMock = vi.fn(async (_url: URL, init?: RequestInit) =>
      json({ ...row, id: "a9", version: 4 }, init?.method === "PATCH" ? 200 : 500),
    );
    vi.stubGlobal("fetch", fetchMock);
    const edits = [{ sheet: "Sheet1", row: 1, col: 2, value: 5 }];
    const created = await call(sheetsRouter.sheets.editTable, {
      artifactId: "a1",
      baseVersion: 3,
      edits,
    });
    expect(created).toMatchObject({ id: "a9", version: 4, mimeType: "text/csv" });
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      base_version: 3,
      edits,
    });
  });

  it("turns a stale base version into a CONFLICT", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ error: { code: "conflict", message: "Stale edit" } }, 409)),
    );
    await expect(
      call(sheetsRouter.sheets.editTable, {
        artifactId: "a1",
        baseVersion: 2,
        edits: [{ sheet: "Sheet1", row: 0, col: 0, value: "x" }],
      }),
    ).rejects.toMatchObject({ code: "CONFLICT" });
  });

  it("reads a range block and maps a bad range to BAD_REQUEST", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: URL) => {
        expect(url.searchParams.get("range")).toBe("C2:C8");
        expect(url.searchParams.get("sheet")).toBe("Sales");
        return json({ artifact_id: "a1", version: 3, block: "[selection ...]" });
      }),
    );
    const input = { artifactId: "a1", sheet: "Sales", range: "C2:C8" };
    expect(await call(sheetsRouter.sheets.tableRange, input)).toEqual({
      version: 3,
      block: "[selection ...]",
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ error: { code: "invalid_input", message: "bad" } }, 400)),
    );
    await expect(call(sheetsRouter.sheets.tableRange, input)).rejects.toMatchObject({
      code: "BAD_REQUEST",
    });
  });
});
