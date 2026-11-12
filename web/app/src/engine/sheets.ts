import { ORPCError } from "@orpc/server";
import {
  editOmnigentTable,
  getOmnigentTable,
  getOmnigentTableRange,
  OmnigentTableConflictError,
} from "@superchat/adapters/omnigent/browser";
import { artifactNotFound, toArtifact } from "./artifact-shape";
import { engineClient, NO_EMAIL } from "./client";
import { os } from "./context";

/** `sheets.*`: a CSV / XLSX deliverable read as grids and hand-edited into new versions. */
export const sheetsRouter = {
  sheets: {
    // The engine's `{v, f, t}` cells, camel-cased.
    table: os.sheets.table.handler(async ({ input }) => {
      const table = await getOmnigentTable(engineClient(), NO_EMAIL, input.artifactId).catch(
        artifactNotFound,
      );
      return {
        artifactId: table.artifact_id,
        version: table.version,
        kind: table.kind,
        sheets: table.sheets.map((sheet) => ({
          name: sheet.name,
          rows: sheet.rows,
          frozenRows: sheet.frozen_rows,
          nRows: sheet.n_rows,
          nCols: sheet.n_cols,
          truncated: sheet.truncated,
          ...(sheet.col_widths ? { colWidths: sheet.col_widths } : {}),
        })),
      };
    }),
    // A stale `baseVersion` is a CONFLICT the web reloads from.
    editTable: os.sheets.editTable.handler(async ({ input }) => {
      try {
        const created = await editOmnigentTable(
          engineClient(),
          NO_EMAIL,
          input.artifactId,
          input.baseVersion,
          input.edits,
        );
        return toArtifact(created, null);
      } catch (error) {
        if (error instanceof OmnigentTableConflictError) {
          throw new ORPCError("CONFLICT", { message: error.message });
        }
        return artifactNotFound(error);
      }
    }),
    tableRange: os.sheets.tableRange.handler(async ({ input }) => {
      try {
        const { version, block } = await getOmnigentTableRange(
          engineClient(),
          NO_EMAIL,
          input.artifactId,
          input.sheet,
          input.range,
        );
        return { version, block };
      } catch (error) {
        if (error instanceof Error && /\(400\)/.test(error.message)) {
          throw new ORPCError("BAD_REQUEST", { message: "That selection can't be read." });
        }
        return artifactNotFound(error);
      }
    }),
  },
};
