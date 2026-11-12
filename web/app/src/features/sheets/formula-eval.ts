import * as formulajs from "@formulajs/formulajs";
import type { TableCell, TableSheet } from "@superchat/contracts";

/** A computed cell value; `undefined` from the evaluator means "not computable here". */
export type FormulaValue = number | string | boolean | null;

class Unsupported extends Error {}

type Ast =
  | { k: "num"; v: number }
  | { k: "str"; v: string }
  | { k: "bool"; v: boolean }
  | { k: "ref"; sheet: string | null; r: number; c: number }
  | { k: "range"; sheet: string | null; r0: number; c0: number; r1: number; c1: number }
  | { k: "un"; op: "-" | "+"; a: Ast }
  | { k: "pct"; a: Ast }
  | { k: "bin"; op: string; a: Ast; b: Ast }
  | { k: "call"; name: string; args: Ast[] };

const REF = /^(?:(?:'([^']+)'|([A-Za-z_][A-Za-z0-9_.]*))!)?\$?([A-Za-z]{1,3})\$?(\d{1,7})/;

const colIndex = (letters: string): number =>
  [...letters.toUpperCase()].reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0) - 1;

type Token = { t: "num" | "str" | "id" | "op" | "ref" | "range"; v: string; ast?: Ast };

function tokenize(src: string): Token[] {
  const out: Token[] = [];
  let i = 0;
  while (i < src.length) {
    const rest = src.slice(i);
    const ch = rest[0] as string;
    if (/\s/.test(ch)) {
      i += 1;
      continue;
    }
    if (ch === '"') {
      const m = /^"((?:[^"]|"")*)"/.exec(rest);
      if (!m) throw new Unsupported();
      out.push({ t: "str", v: (m[1] ?? "").replace(/""/g, '"') });
      i += m[0].length;
      continue;
    }
    const num = /^(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?/.exec(rest);
    if (num && !/^\d+[A-Za-z]/.test(rest)) {
      out.push({ t: "num", v: num[0] });
      i += num[0].length;
      continue;
    }
    const ref = REF.exec(rest);
    if (ref && !/^[A-Za-z0-9_(]/.test(rest.slice(ref[0].length))) {
      const sheet = ref[1] ?? ref[2] ?? null;
      const r = Number(ref[4]) - 1;
      const c = colIndex(ref[3] as string);
      const next = REF.exec(rest.slice(ref[0].length + 1));
      if (rest[ref[0].length] === ":" && next && !next[1] && !next[2]) {
        const r1 = Number(next[4]) - 1;
        const c1 = colIndex(next[3] as string);
        out.push({
          t: "range",
          v: "",
          ast: {
            k: "range",
            sheet,
            r0: Math.min(r, r1),
            c0: Math.min(c, c1),
            r1: Math.max(r, r1),
            c1: Math.max(c, c1),
          },
        });
        i += ref[0].length + 1 + next[0].length;
      } else {
        out.push({ t: "ref", v: "", ast: { k: "ref", sheet, r, c } });
        i += ref[0].length;
      }
      continue;
    }
    const id = /^[A-Za-z_][A-Za-z0-9_.]*/.exec(rest);
    if (id) {
      out.push({ t: "id", v: id[0].toUpperCase() });
      i += id[0].length;
      continue;
    }
    const op = /^(<>|<=|>=|[-+*/^&=<>(),%])/.exec(rest);
    if (!op) throw new Unsupported();
    out.push({ t: "op", v: op[0] });
    i += op[0].length;
  }
  return out;
}

function parse(src: string): Ast {
  const tokens = tokenize(src);
  let p = 0;
  const peek = () => tokens[p];
  const isOp = (v: string) => peek()?.t === "op" && peek()?.v === v;
  const level = (ops: string[], next: () => Ast): Ast => {
    let left = next();
    while (peek()?.t === "op" && ops.includes(peek()?.v as string)) {
      const op = tokens[p++]?.v as string;
      left = { k: "bin", op, a: left, b: next() };
    }
    return left;
  };
  const primary = (): Ast => {
    const tok = tokens[p++];
    if (!tok) throw new Unsupported();
    if (tok.t === "num") return { k: "num", v: Number(tok.v) };
    if (tok.t === "str") return { k: "str", v: tok.v };
    if ((tok.t === "ref" || tok.t === "range") && tok.ast) return tok.ast;
    if (tok.t === "op" && tok.v === "(") {
      const inner = comparison();
      if (!isOp(")")) throw new Unsupported();
      p += 1;
      return inner;
    }
    if (tok.t === "id") {
      if (tok.v === "TRUE" || tok.v === "FALSE") return { k: "bool", v: tok.v === "TRUE" };
      if (!isOp("(")) throw new Unsupported();
      p += 1;
      const args: Ast[] = [];
      if (isOp(")")) p += 1;
      else {
        for (;;) {
          args.push(comparison());
          if (isOp(",")) p += 1;
          else if (isOp(")")) {
            p += 1;
            break;
          } else throw new Unsupported();
        }
      }
      return { k: "call", name: tok.v, args };
    }
    throw new Unsupported();
  };
  const postfix = (): Ast => {
    let a = primary();
    while (isOp("%")) {
      p += 1;
      a = { k: "pct", a };
    }
    return a;
  };
  const unary = (): Ast => {
    if (isOp("-") || isOp("+")) {
      const op = tokens[p++]?.v as "-" | "+";
      return { k: "un", op, a: unary() };
    }
    return postfix();
  };
  const power = (): Ast => level(["^"], unary);
  const product = (): Ast => level(["*", "/"], power);
  const sum = (): Ast => level(["+", "-"], product);
  const concat = (): Ast => level(["&"], sum);
  function comparison(): Ast {
    return level(["=", "<>", "<", ">", "<=", ">="], concat);
  }
  const ast = comparison();
  if (p !== tokens.length) throw new Unsupported();
  return ast;
}

const FUNCTIONS: Record<string, (...args: unknown[]) => unknown> = {
  SUM: formulajs.SUM as never,
  AVERAGE: formulajs.AVERAGE as never,
  MIN: formulajs.MIN as never,
  MAX: formulajs.MAX as never,
  COUNT: formulajs.COUNT as never,
  COUNTA: formulajs.COUNTA as never,
  ROUND: formulajs.ROUND as never,
  ABS: formulajs.ABS as never,
};

type Operand = FormulaValue | FormulaValue[];

/**
 * Evaluates the formulas of loaded sheets in the browser: arithmetic, comparisons, cell and range
 * refs (also `Sheet!A1`), and SUM / AVERAGE / MIN / MAX / COUNT / COUNTA / ROUND / ABS / IF.
 * Results are memoised; a circular reference or anything unsupported is `undefined`, so the
 * caller shows the formula text instead. `override` supplies edited cells not yet saved.
 */
export function createEvaluator(
  sheets: readonly TableSheet[],
  override?: (sheet: string, r: number, c: number) => TableCell | undefined,
): (sheet: string, r: number, c: number) => FormulaValue | undefined {
  const memo = new Map<string, FormulaValue | undefined>();
  const parsed = new Map<string, Ast | null>();
  const active = new Set<string>();
  const byName = new Map(sheets.map((s) => [s.name.toLowerCase(), s]));

  const rawCell = (sheet: TableSheet, r: number, c: number): TableCell | undefined =>
    override?.(sheet.name, r, c) ?? sheet.rows[r]?.[c];

  function valueAt(sheet: TableSheet, r: number, c: number): FormulaValue {
    const cell = rawCell(sheet, r, c);
    if (!cell) return null;
    if (!cell.f) return cell.v;
    const computed = compute(sheet, r, c);
    if (computed !== undefined) return computed;
    if (cell.v === null) throw new Unsupported();
    return cell.v;
  }

  function compute(sheet: TableSheet, r: number, c: number): FormulaValue | undefined {
    const key = `${sheet.name}\u0000${r}:${c}`;
    if (memo.has(key)) return memo.get(key);
    const cell = rawCell(sheet, r, c);
    if (!cell?.f) return undefined;
    if (active.has(key)) throw new Unsupported(); // circular
    active.add(key);
    let result: FormulaValue | undefined;
    try {
      let ast = parsed.get(cell.f);
      if (ast === undefined) {
        try {
          ast = parse(cell.f.replace(/^=/, ""));
        } catch {
          ast = null;
        }
        parsed.set(cell.f, ast);
      }
      if (ast) {
        const out = evaluate(ast, sheet);
        if (Array.isArray(out)) throw new Unsupported();
        result = typeof out === "number" && !Number.isFinite(out) ? undefined : out;
      }
    } catch {
      result = undefined;
    } finally {
      active.delete(key);
    }
    memo.set(key, result);
    return result;
  }

  const sheetFor = (name: string | null, home: TableSheet): TableSheet => {
    if (name === null) return home;
    const found = byName.get(name.toLowerCase());
    if (!found) throw new Unsupported();
    return found;
  };

  const num = (v: Operand): number => {
    if (Array.isArray(v)) throw new Unsupported();
    if (v === null || v === "") return 0;
    if (typeof v === "boolean") return v ? 1 : 0;
    const n = typeof v === "number" ? v : Number(v);
    if (Number.isNaN(n)) throw new Unsupported();
    return n;
  };

  function evaluate(ast: Ast, home: TableSheet): Operand {
    switch (ast.k) {
      case "num":
      case "str":
      case "bool":
        return ast.v;
      case "ref": {
        const sheet = sheetFor(ast.sheet, home);
        return valueAt(sheet, ast.r, ast.c);
      }
      case "range": {
        const sheet = sheetFor(ast.sheet, home);
        if ((ast.r1 - ast.r0 + 1) * (ast.c1 - ast.c0 + 1) > 50_000) throw new Unsupported();
        const values: FormulaValue[] = [];
        for (let r = ast.r0; r <= ast.r1; r += 1) {
          for (let c = ast.c0; c <= ast.c1; c += 1) values.push(valueAt(sheet, r, c));
        }
        return values;
      }
      case "un":
        return ast.op === "-" ? -num(evaluate(ast.a, home)) : num(evaluate(ast.a, home));
      case "pct":
        return num(evaluate(ast.a, home)) / 100;
      case "bin": {
        const a = evaluate(ast.a, home);
        const b = evaluate(ast.b, home);
        switch (ast.op) {
          case "+":
            return num(a) + num(b);
          case "-":
            return num(a) - num(b);
          case "*":
            return num(a) * num(b);
          case "/": {
            if (num(b) === 0) throw new Unsupported();
            return num(a) / num(b);
          }
          case "^":
            return num(a) ** num(b);
          case "&":
            return `${scalar(a) ?? ""}${scalar(b) ?? ""}`;
          default:
            return compare(ast.op, scalar(a), scalar(b));
        }
      }
      case "call": {
        if (ast.name === "IF") {
          if (ast.args.length < 2 || ast.args.length > 3) throw new Unsupported();
          const cond = scalar(evaluate(ast.args[0] as Ast, home));
          const truthy = typeof cond === "string" ? cond !== "" : Boolean(cond);
          const pick = truthy ? ast.args[1] : ast.args[2];
          return pick ? evaluate(pick, home) : false;
        }
        const fn = FUNCTIONS[ast.name];
        if (!fn) throw new Unsupported();
        const out = fn(...ast.args.map((arg) => evaluate(arg, home)));
        if (out instanceof Error || (typeof out === "number" && Number.isNaN(out))) {
          throw new Unsupported();
        }
        if (typeof out !== "number" && typeof out !== "string" && typeof out !== "boolean") {
          throw new Unsupported();
        }
        return out;
      }
    }
  }

  return (sheetName, r, c) => {
    const sheet = byName.get(sheetName.toLowerCase());
    return sheet ? compute(sheet, r, c) : undefined;
  };
}

function scalar(v: Operand): FormulaValue {
  if (Array.isArray(v)) throw new Unsupported();
  return v;
}

function compare(op: string, a: FormulaValue, b: FormulaValue): boolean {
  const x = a ?? (typeof b === "string" ? "" : 0);
  const y = b ?? (typeof a === "string" ? "" : 0);
  const [l, r] =
    typeof x === typeof y ? [x, y] : [typeof x === "string" ? 1 : 0, typeof y === "string" ? 1 : 0];
  switch (op) {
    case "=":
      return l === r;
    case "<>":
      return l !== r;
    case "<":
      return l < r;
    case ">":
      return l > r;
    case "<=":
      return l <= r;
    default:
      return l >= r;
  }
}
