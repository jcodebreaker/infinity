import type { TableCell } from "@superchat/contracts";
import { describe, expect, it } from "vitest";
import { formatCellValue, serialToParts } from "./number-format";

const n = (v: number, z?: string): TableCell => ({ v, f: null, t: "n", ...(z ? { z } : {}) });
const d = (v: string, z: string): TableCell => ({ v, f: null, t: "d", z });

describe("formatCellValue", () => {
  it("formats thousands, decimals and percent", () => {
    expect(formatCellValue(n(1234567.891, "#,##0"))).toBe("1,234,568");
    expect(formatCellValue(n(1234.5, "#,##0.00"))).toBe("1,234.50");
    expect(formatCellValue(n(3.14159, "0.00"))).toBe("3.14");
    expect(formatCellValue(n(0.256, "0%"))).toBe("26%");
    expect(formatCellValue(n(0.2565, "0.0%"))).toBe("25.7%");
    expect(formatCellValue(n(5, "000"))).toBe("005");
    expect(formatCellValue(n(-1234.5, "#,##0.00"))).toBe("-1,234.50");
    expect(formatCellValue(n(1500000, '#,##0,,"M"'))).toBe("2M");
  });

  it("formats currency with a symbol, [$...] and negative sections", () => {
    expect(formatCellValue(n(64000, "$#,##0.00"))).toBe("$64,000.00");
    expect(formatCellValue(n(64000, '"$"#,##0'))).toBe("$64,000");
    expect(formatCellValue(n(1234.5, "[$€-407] #,##0.00"))).toBe("€ 1,234.50");
    expect(formatCellValue(n(-50, "$#,##0;($#,##0)"))).toBe("($50)");
    expect(formatCellValue(n(50, "$#,##0_);[Red]($#,##0)"))).toBe("$50 ");
    expect(formatCellValue(n(0, '0;-0;"zero"'))).toBe("zero");
  });

  it("formats ISO dates and Excel serials", () => {
    expect(formatCellValue(d("2024-03-01T00:00:00", "yyyy-mm-dd"))).toBe("2024-03-01");
    expect(formatCellValue(d("2024-03-01", "m/d/yyyy"))).toBe("3/1/2024");
    expect(formatCellValue(d("2024-03-09T14:05:00", "d mmm yyyy h:mm AM/PM"))).toBe(
      "9 Mar 2024 2:05 PM",
    );
    expect(formatCellValue(n(45352, "yyyy-mm-dd"))).toBe("2024-03-01");
    expect(formatCellValue(n(45352.5, "yyyy-mm-dd hh:mm"))).toBe("2024-03-01 12:00");
    expect(formatCellValue(n(1, "mmmm d, yyyy"))).toBe("January 1, 1900");
    expect(serialToParts(61)).toMatchObject({ y: 1900, mo: 3, d: 1 });
  });

  it("falls back to raw for General, text, fractions and unknown codes", () => {
    expect(formatCellValue(n(5, "General"))).toBeNull();
    expect(formatCellValue(n(5))).toBeNull();
    expect(formatCellValue(n(0.5, "# ?/?"))).toBeNull();
    expect(formatCellValue(n(5, "@"))).toBeNull();
    expect(formatCellValue({ v: "abc", f: null, t: "s", z: "0.00" })).toBeNull();
    expect(formatCellValue({ v: true, f: null, t: "b", z: "0.00" })).toBeNull();
  });
});
