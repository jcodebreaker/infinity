import { Trans, useLingui } from "@lingui/react/macro";
import type { EngineModelConnection } from "@superchat/contracts";
import { Button } from "@superchat/ui-web";
import { KeyRound } from "lucide-react";
import { useCallback, useEffect, useId, useState } from "react";
import { SettingsGroup, SettingsRow } from "../../components/settings/kit";
import { errorText } from "../../lib/error-text";
import { rpc } from "../../lib/rpc";
import { formatEngineDate, useModelProviders } from "./engine-models";

const FIELD =
  "min-w-0 flex-1 rounded-lg bg-muted px-2.5 py-1.5 text-[14px] text-foreground outline-none placeholder:text-ink-3 focus-visible:ring-2 focus-visible:ring-ring";

/**
 * A key form: one password field. The engine checks the key with the provider before it keeps
 * it, so a rejected key comes back here as the error under the field.
 */
export function KeyForm({
  providerLabel,
  onSave,
  onCancel,
}: {
  providerLabel: string;
  onSave: (apiKey: string) => Promise<void>;
  onCancel: () => void;
}) {
  const { t } = useLingui();
  const id = useId();
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    if (!key.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onSave(key.trim());
    } catch (err) {
      setError(errorText(err, t`Could not save this key.`));
      setBusy(false);
    }
  }

  return (
    <form
      className="flex flex-col gap-1.5 px-3 pb-3"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <div className="flex items-center gap-2">
        <input
          id={id}
          type="password"
          name={`${providerLabel}-api-key`}
          aria-label={t`${providerLabel} API key`}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? `${id}-error` : undefined}
          autoComplete="off"
          autoCapitalize="off"
          spellCheck={false}
          // biome-ignore lint/a11y/noAutofocus: the person just chose to add a key
          autoFocus
          value={key}
          onChange={(event) => setKey(event.target.value)}
          placeholder="sk-…"
          className={FIELD}
        />
        <Button type="submit" size="sm" disabled={!key.trim() || busy}>
          {busy ? <Trans>Checking…</Trans> : <Trans>Save</Trans>}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={busy}>
          <Trans>Cancel</Trans>
        </Button>
      </div>
      {error ? (
        <p id={`${id}-error`} role="alert" className="text-[12.5px] text-destructive">
          {error}
        </p>
      ) : null}
    </form>
  );
}

/**
 * The person's provider keys. A person with no key of their own who can still use a model is on
 * their organization's: shown as a read-only line (`orgProvides`).
 */
export function ModelKeys({
  orgProvides = false,
  onChanged,
}: {
  /** The person can run models without a key of their own. */
  orgProvides?: boolean;
  onChanged?: () => void;
}) {
  const { t, i18n } = useLingui();
  const providers = useModelProviders();
  const [rows, setRows] = useState<EngineModelConnection[] | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setRows(await rpc.engineModels.connections());
    } catch {
      setRows([]);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  if (!rows || !providers) return null;

  async function save(provider: string, apiKey: string) {
    await rpc.engineModels.connect({ provider, apiKey });
    setEditing(null);
    await load();
    onChanged?.();
  }

  async function remove(provider: string) {
    setError(null);
    setRemoving(provider);
    try {
      await rpc.engineModels.disconnect({ provider });
      await load();
      onChanged?.();
    } catch (err) {
      setError(errorText(err, t`Could not remove this key.`));
    } finally {
      setRemoving(null);
    }
  }

  const hasOwn = rows.length > 0;
  return (
    <SettingsGroup title={<Trans>API keys</Trans>}>
      {providers.map((provider) => {
        const row = rows.find((candidate) => candidate.provider === provider.provider);
        const checked = row ? formatEngineDate(row.validatedAt, i18n.locale) : "";
        const hint = row?.hint ?? "";
        const sub = !row
          ? t`Not connected`
          : row.status !== "valid"
            ? t`••••${hint} · Needs attention`
            : checked
              ? t`••••${hint} · Checked ${checked}`
              : `••••${hint}`;
        return (
          <div key={provider.provider} data-testid={`model-key-${provider.provider}`}>
            <SettingsRow
              icon={{ tone: "indigo", glyph: <KeyRound strokeWidth={2.4} /> }}
              label={provider.label}
              sub={sub}
            >
              <span className="flex shrink-0 items-center gap-1">
                {row ? (
                  <>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() =>
                        setEditing(editing === provider.provider ? null : provider.provider)
                      }
                    >
                      <Trans>Replace</Trans>
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="text-destructive"
                      disabled={removing === provider.provider}
                      onClick={() => void remove(provider.provider)}
                    >
                      <Trans>Remove</Trans>
                    </Button>
                  </>
                ) : (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() =>
                      setEditing(editing === provider.provider ? null : provider.provider)
                    }
                  >
                    <Trans>Add key</Trans>
                  </Button>
                )}
              </span>
            </SettingsRow>
            {editing === provider.provider ? (
              <KeyForm
                providerLabel={provider.label}
                onSave={(apiKey) => save(provider.provider, apiKey)}
                onCancel={() => setEditing(null)}
              />
            ) : null}
          </div>
        );
      })}
      {!hasOwn && orgProvides ? (
        <SettingsRow
          label={<Trans>Provided by your organization</Trans>}
          icon={{ tone: "gray", glyph: <KeyRound strokeWidth={2.4} /> }}
        />
      ) : null}
      {error ? (
        <p role="alert" className="px-3 pb-2 text-[12.5px] text-destructive">
          {error}
        </p>
      ) : null}
    </SettingsGroup>
  );
}
