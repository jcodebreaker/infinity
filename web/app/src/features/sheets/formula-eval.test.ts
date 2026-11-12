import type { TableCell, TableSheet } from "@superchat/contracts";
import { describe, expect, it } from "vitest";
import { createEvaluator } from "./formula-eval";

const n = (v: number): TableCell => ({ v, f: null, t: "n" });
const s = (v: string): TableCell => ({ v, f: null, t: "s" });
const f = (formula: string, cached: number | null = null): TableCell => ({
  v: cached,
  f: formula,
  t: null,
});
const sheet = (name: string, rows: TableCell[][]): TableSheet => ({
  name,
  rows,
  frozenRows: 0,
  nRows: rows.length,
  nCols: rows[0]?.length ?? 0,
  truncated: false,
});

const sales = sheet("Sales", [
  [s("Item"), s("Amount")],
  [s("a"), n(10)],
  [s("b"), n(20.5)],
  [s("c"), n(30)],
  [s("Total"), f("=SUM(B2:B4)")],
  [s("Avg"), f("=ROUND(AVERAGE(B2:B4),1)")],
  [s("Nested"), f("=IF(B5>50,MAX(B2:B4)*2,MIN(B2:B4))")],
  [s("Math"), f("=(1+2)*3^2-4/2")],
  [s("Counts"), f("=COUNT(A2:B4)+COUNTA(A2:B4)")],
]);

describe("formula evaluator", () => {
  const run = createEvaluator([sales]);
  it("sums ranges", () => expect(run("Sales", 4, 1)).toBe(60.5));
  it("nests functions and rounds", () => {
    expect(run("Sales", 5, 1)).toBe(20.2);
    expect(run("Sales", 6, 1)).toBe(60);
  });
  it("does arithmetic with precedence and parentheses", () => expect(run("Sales", 7, 1)).toBe(25));
  it("counts numbers and non-empty cells", () => expect(run("Sales", 8, 1)).toBe(3 + 6));

  it("reads cells on other loaded sheets", () => {
    const other = sheet("By region", [[f("=Sales!B2+'Sales'!B3"), f("=SUM(Sales!B2:B4)*2")]]);
    const cross = createEvaluator([sales, other]);
    expect(cross("By region", 0, 0)).toBe(30.5);
    expect(cross("By region", 0, 1)).toBe(121);
  });

  it("recomputes dependents after an edit", () => {
    const edited = createEvaluator([sales], (name, r, c) =>
      name === "Sales" && r === 1 && c === 1 ? n(100) : undefined,
    );
    expect(edited("Sales", 4, 1)).toBe(150.5);
  });

  it("ignores a stale cached value when it can compute", () => {
    const stale = sheet("S", [[n(1), n(2), f("=A1+B1", 99)]]);
    expect(createEvaluator([stale])("S", 0, 2)).toBe(3);
  });

  it("gives up on circular references", () => {
    const loop = sheet("L", [[f("=B1+1"), f("=A1+1")], [f("=A2+1")]]);
    const run2 = createEvaluator([loop]);
    expect(run2("L", 0, 0)).toBeUndefined();
    expect(run2("L", 1, 0)).toBeUndefined();
  });

  it("falls back for unsupported functions, bad syntax, unknown sheets and division by zero", () => {
    const odd = sheet("O", [
      [f("=VLOOKUP(1,A1:B2,2,FALSE)"), f("=1+"), f("=Nope!A1"), f("=1/0"), f("=SUM(A2)", 7)],
      [s("x")],
    ]);
    const run3 = createEvaluator([odd]);
    for (let c = 0; c < 4; c += 1) expect(run3("O", 0, c)).toBeUndefined();
    // Not computable and no cached value is undefined; the cached one stays with the cell.
    expect(run3("O", 0, 4)).toBe(0);
  });
});
