import { expect, it } from "vitest";
import { Committer } from "./committer";
import {
  coalescePatches,
  firstFamily,
  px,
  readTranslate,
  stylePatches,
  toHex,
  withTranslate,
} from "./edit-model";
import { sanitizeField } from "./sanitize";

it("reads colours and lengths", () => {
  expect(toHex("rgb(30, 43, 250)")).toBe("#1e2bfa");
  expect(toHex("rgba(0, 0, 0, 0)")).toBe("");
  expect(toHex("#ABCDEF")).toBe("#abcdef");
  expect(px("64px")).toBe(64);
  expect(px("normal")).toBeNull();
  expect(firstFamily('"Space Grotesk", sans-serif')).toBe("Space Grotesk");
});

it("nudges through translate() and keeps other transform functions", () => {
  expect(readTranslate("translate(10px, -4px) rotate(2deg)")).toEqual({ x: 10, y: -4 });
  expect(withTranslate("rotate(2deg)", { x: 3, y: 0 })).toBe("translate(3px, 0px) rotate(2deg)");
  expect(withTranslate("translate(3px, 1px)", { x: 0, y: 0 })).toBeNull();
});

it("merges neighbouring style patches per element, later wins, first before is kept", () => {
  const t = { id: "a", inline: {}, computed: { "font-size": "10px" } } as never;
  const [one] = stylePatches([t], { "font-size": "11px" });
  const [two] = stylePatches([t], { "font-size": "12px", color: "red" });
  if (!one || !two) throw new Error("patches");
  expect(coalescePatches([one, two])).toEqual([
    {
      kind: "set-style",
      id: "a",
      style: { "font-size": "12px", color: "red" },
      before: { "font-size": "10px" },
    },
  ]);
  expect(
    coalescePatches([one, { kind: "remove-element", id: "b" }, two]).map((p) => p.kind),
  ).toEqual(["set-style", "remove-element", "set-style"]);
});

it("sanitizes untrusted deck text like the Design Mode sanitizer", () => {
  expect(sanitizeField("  a\u0000b\n\tc d  ")).toBe("a b c d");
  expect(sanitizeField('<b>"x"</b>')).toBe("‹b›'x'‹/b›");
  expect(sanitizeField("x".repeat(500))).toHaveLength(200);
  expect(sanitizeField(undefined)).toBe("");
});

it("the committer batches: a drag is one save, saves run one at a time", async () => {
  const batches: unknown[][] = [];
  let release: (() => void) | undefined;
  const committer = new Committer(async (patches) => {
    batches.push(patches);
    if (batches.length === 1) await new Promise<void>((resolve) => (release = resolve));
    return { ok: true };
  }, 20);
  const style = (n: number) =>
    ({ kind: "set-style", id: "a", style: { opacity: String(n) } }) as const;
  for (let n = 0; n < 30; n++) committer.stage([style(n / 30)]);
  await new Promise((r) => setTimeout(r, 40));
  expect(batches).toHaveLength(1);
  expect(batches[0]).toHaveLength(1); // 30 drags, one patch
  committer.stage([style(1)]);
  const second = committer.commit();
  expect(batches).toHaveLength(1); // waits for the save in flight
  release?.();
  await second;
  expect(batches).toHaveLength(2);
});

it("a refused save drops what was queued behind it", async () => {
  const saves: number[] = [];
  const committer = new Committer(async () => {
    saves.push(1);
    return { ok: false, message: "no" };
  }, 10);
  const a = committer.commit([{ kind: "remove-element", id: "a" }]);
  committer.stage([{ kind: "remove-element", id: "b" }]);
  await a;
  await committer.commit();
  expect(saves).toHaveLength(1);
});
