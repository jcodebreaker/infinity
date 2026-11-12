import type { Automation } from "@superchat/contracts";
import { useMemo } from "react";
import { AutomationsSheet } from "../../features/automations";
import type { AutomationsWire } from "../../features/automations/useAutomations";
import { DEV_AUTOMATIONS, DEV_SYSTEM_JOBS } from "./automations-fixture";

/**
 * Dev-only route (`/dev/automations`, gated by `import.meta.env.DEV`): the real Automations
 * sheet on an in-memory fixture, so the sheet is reviewed without touching any engine's
 * scheduled tasks. `?empty` shows the empty state.
 */
export function AutomationsPreviewPage() {
  const wire = useMemo<AutomationsWire>(() => {
    let rows: Automation[] = new URLSearchParams(window.location.search).has("empty")
      ? []
      : [...DEV_AUTOMATIONS];
    const find = (id: string) => rows.find((row) => row.id === id) as Automation;
    return {
      list: async () => rows,
      system: async () => DEV_SYSTEM_JOBS,
      create: async (input) => {
        const row: Automation = {
          id: `auto-${rows.length + 1}`,
          name: input.name,
          prompt: input.prompt,
          rrule: "",
          schedule: input.schedule,
          timezone: input.timezone ?? "UTC",
          enabled: true,
          nextRunAt: new Date(Date.now() + 3_600_000).toISOString(),
          lastRunAt: null,
          lastRunStatus: null,
          model: input.model ?? null,
          createdAt: new Date().toISOString(),
        };
        rows = [row, ...rows];
        return row;
      },
      update: async (input) => {
        const row = {
          ...find(input.id),
          ...(input.enabled === undefined ? {} : { enabled: input.enabled }),
        };
        rows = rows.map((item) => (item.id === row.id ? row : item));
        return row;
      },
      run: async () => ({ ok: true as const }),
      remove: async (input) => {
        rows = rows.filter((row) => row.id !== input.id);
        return { ok: true as const };
      },
    };
  }, []);
  return (
    <div className="h-screen bg-background">
      <AutomationsSheet botId="dev-bot" open onClose={() => undefined} wire={wire} />
    </div>
  );
}
