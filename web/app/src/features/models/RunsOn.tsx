import { Trans, useLingui } from "@lingui/react/macro";
import type { EngineHarnessId } from "@superchat/contracts";
import { useState } from "react";
import { SettingsGroup, SettingsSegmented } from "../../components/settings/kit";
import { errorText } from "../../lib/error-text";
import { rpc } from "../../lib/rpc";

/** The harness the person's Superchat runs on, and the switch to the other one. The pick shows at
 * once and goes back if the engine refuses; `onChanged` asks for the status again, so the model
 * rows follow the new harness (the parent remounts this by `harness`). */
export function RunsOn({
  harness,
  onChanged,
}: {
  harness: EngineHarnessId;
  onChanged: () => void;
}) {
  const { t } = useLingui();
  const [chosen, setChosen] = useState<EngineHarnessId | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function choose(next: EngineHarnessId) {
    setChosen(next);
    setSaving(true);
    setError(null);
    try {
      await rpc.engineModels.setHarness({ harness: next });
      onChanged();
    } catch (err) {
      setChosen(null);
      setError(errorText(err, t`Could not save.`));
    } finally {
      setSaving(false);
    }
  }

  return (
    <SettingsGroup title={<Trans>Runs on</Trans>}>
      <SettingsSegmented
        label={t`Runs on`}
        testId="runs-on"
        value={chosen ?? harness}
        disabled={saving}
        options={[
          { value: "claude-sdk", label: t`Claude` },
          { value: "pi", label: t`Pi` },
        ]}
        onChange={(next) => void choose(next)}
      />
      {error ? (
        <p role="alert" className="px-3 pb-2 text-[12.5px] text-destructive">
          {error}
        </p>
      ) : null}
    </SettingsGroup>
  );
}
