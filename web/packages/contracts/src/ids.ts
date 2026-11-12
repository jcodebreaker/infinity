import * as z from "zod";

export const Id = z.string().min(1);

export const RunStatus = z.enum([
  "queued",
  "leased",
  "running",
  "waiting_input",
  "completed",
  "failed",
  "cancelled",
]);
export type RunStatus = z.infer<typeof RunStatus>;
