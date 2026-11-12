import { oc } from "@orpc/contract";
import * as z from "zod";
import { Id } from "../ids.js";
import { ArtifactSchema } from "./artifacts.js";

/** One cell of a CSV / XLSX artifact; `v` is null for a formula with no cached value. */
export const TableCellSchema = z.object({
  v: z.union([z.string(), z.number(), z.boolean()]).nullable(),
  f: z.string().nullable(),
  t: z.string().nullable(),
  /** The cell's Excel number format, when it is not "General" (XLSX only). */
  z: z.string().optional(),
});
export type TableCell = z.infer<typeof TableCellSchema>;

export const TableSheetSchema = z.object({
  name: z.string(),
  rows: z.array(z.array(TableCellSchema)),
  frozenRows: z.number().int(),
  nRows: z.number().int(),
  nCols: z.number().int(),
  truncated: z.boolean(),
  /** Column widths in characters (null = default), index = column; absent when none are set. */
  colWidths: z.array(z.number().nullable()).optional(),
});
export type TableSheet = z.infer<typeof TableSheetSchema>;

export const ArtifactTableSchema = z.object({
  artifactId: Id,
  version: z.number().int(),
  kind: z.string(),
  sheets: z.array(TableSheetSchema),
});
export type ArtifactTable = z.infer<typeof ArtifactTableSchema>;

export const TableCellEditSchema = z.object({
  sheet: z.string().max(256),
  row: z.number().int().min(0),
  col: z.number().int().min(0),
  value: z.union([z.string(), z.number(), z.boolean()]).nullable(),
});
export type TableCellEdit = z.infer<typeof TableCellEditSchema>;

export const sheetsContract = {
  sheets: {
    /** A CSV / XLSX version as grids. */
    table: oc.input(z.object({ artifactId: Id })).output(ArtifactTableSchema),
    /** Cell edits as a new manual version; CONFLICT when `baseVersion` is no longer the newest. */
    editTable: oc
      .input(
        z.object({
          artifactId: Id,
          baseVersion: z.number().int().min(1),
          edits: z.array(TableCellEditSchema).min(1).max(500),
        }),
      )
      .output(ArtifactSchema),
    /** The untrusted-data excerpt of a selection, to attach to the person's next message. */
    tableRange: oc
      .input(
        z.object({ artifactId: Id, sheet: z.string().max(256), range: z.string().min(1).max(64) }),
      )
      .output(z.object({ version: z.number().int(), block: z.string() })),
  },
};
