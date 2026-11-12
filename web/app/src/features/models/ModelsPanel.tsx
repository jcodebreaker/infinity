import type { EngineModelsStatus } from "@superchat/contracts";
import { EngineHarnessIdSchema } from "@superchat/contracts";
import { useState } from "react";
import { BudgetGroup } from "./BudgetGroup";
import { DefaultModel } from "./DefaultModel";
import { ModelKeys } from "./ModelKeys";
import { RunsOn } from "./RunsOn";

/** Settings > Models: the person's provider keys, the harness their Superchat runs on, the model
 * new chats start on, and their monthly budget. Everything here is the engine's (the keys are
 * probed and sealed there). */
export function ModelsPanel({
  status,
  onChanged,
}: {
  status: EngineModelsStatus;
  /** A key was added or removed, or the harness changed: the engine's answer may have changed. */
  onChanged: () => void;
}) {
  const [keysVersion, setKeysVersion] = useState(0);
  // Known only once the engine names the one harness the Superchat runs on.
  const harness =
    status.harnesses.length === 1 ? EngineHarnessIdSchema.safeParse(status.harnesses[0]?.id) : null;
  return (
    <div data-testid="models-panel" className="flex flex-col gap-6">
      <ModelKeys
        orgProvides={status.ready}
        onChanged={() => {
          onChanged();
          setKeysVersion((version) => version + 1);
        }}
      />
      {harness?.success ? (
        <RunsOn key={harness.data} harness={harness.data} onChanged={onChanged} />
      ) : null}
      <DefaultModel key={keysVersion} harnesses={status.harnesses} />
      <BudgetGroup />
    </div>
  );
}
