import { Trans, useLingui } from "@lingui/react/macro";
import { Mail, UserRound } from "lucide-react";
import { useState } from "react";
import { SettingsGroup, SettingsRow, SettingsSegmented } from "../../../components/settings/kit";
import {
  type AppearancePreference,
  getUiAppearancePreference,
  setUiAppearance,
} from "../../../lib/ui-appearance";

/** Settings > General: the account, how the app looks. */
export function GeneralPanel({ name, email }: { name: string; email?: string | null }) {
  const { t } = useLingui();
  const [appearance, setAppearance] = useState<AppearancePreference>(() =>
    getUiAppearancePreference(),
  );

  return (
    <div className="flex flex-col gap-6">
      <SettingsGroup title={<Trans>Account</Trans>}>
        <SettingsRow
          icon={{ tone: "blue", glyph: <UserRound strokeWidth={2.4} /> }}
          label={<Trans>Name</Trans>}
          value={name}
        />
        {email ? (
          <SettingsRow
            icon={{ tone: "blue", glyph: <Mail strokeWidth={2.4} /> }}
            label={<Trans>Email</Trans>}
            value={email}
          />
        ) : null}
      </SettingsGroup>

      <SettingsGroup title={<Trans>Appearance</Trans>}>
        <SettingsSegmented
          label={t`Appearance`}
          testId="ui-appearance"
          value={appearance}
          options={[
            { value: "system", label: t`System` },
            { value: "light", label: t`Light` },
            { value: "dark", label: t`Dark` },
          ]}
          onChange={(next) => {
            setAppearance(next);
            setUiAppearance(next);
          }}
        />
      </SettingsGroup>
    </div>
  );
}
