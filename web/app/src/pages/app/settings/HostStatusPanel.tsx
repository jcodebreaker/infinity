import { useLingui } from "@lingui/react/macro";
import type { HostStatus } from "@superchat/contracts";
import { useCallback, useEffect, useState } from "react";
import { SettingsGroup, SettingsRow } from "../../../components/settings/kit";
import { rpc } from "../../../lib/rpc";

type SessionModel = { harness: string; model: string | null; defaultModel: string | null };

/** The calls the panel makes; the live RPC unless a test supplies its own. */
export type HostStatusWire = {
  status: (input: { botId: string }) => Promise<HostStatus>;
  sessionModel: (input: { botId: string }) => Promise<{
    harness: string;
    model: string | null;
    catalog: { defaultModel?: string | null; models: { id: string; label: string }[] };
  }>;
};

/**
 * Made once: the RPC client hands out a new `rpc.host` object on every read, so using it
 * as a default would change the effect's deps each render and reload forever.
 */
const LIVE_WIRE: HostStatusWire = {
  status: (input) => rpc.host.status(input),
  sessionModel: (input) => rpc.engineModels.sessionModel(input),
};

const POLL_MS = 10_000;

const HARNESS_LABEL: Record<string, string> = { "claude-sdk": "Claude Agent SDK", pi: "Pi" };

/** Settings > Machine: whether the host is up, and the harness and model the Conversation runs on. */
export function HostStatusPanel({
  botId,
  wire = LIVE_WIRE,
}: {
  botId: string;
  wire?: HostStatusWire;
}) {
  const { t } = useLingui();
  const [status, setStatus] = useState<HostStatus | null>(null);
  const [model, setModel] = useState<SessionModel | null>(null);
  const [failed, setFailed] = useState(false);

  const load = useCallback(async () => {
    const [s, m] = await Promise.allSettled([wire.status({ botId }), wire.sessionModel({ botId })]);
    if (s.status === "fulfilled") setStatus(s.value);
    if (m.status === "fulfilled") {
      const { catalog } = m.value;
      const id = catalog.defaultModel ?? "";
      setModel({
        harness: m.value.harness,
        model: m.value.model,
        defaultModel: catalog.models.find((x) => x.id === id)?.label ?? null,
      });
    }
    setFailed(s.status === "rejected");
  }, [botId, wire]);

  useEffect(() => {
    let cancelled = false;
    const tick = () => {
      if (!cancelled) void load();
    };
    tick();
    const timer = setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [load]);

  const stateLabel = (() => {
    if (!status) return failed ? t`Unavailable` : t`Loading…`;
    if (status.launch) return status.launch.stage === "failed" ? t`Failed to start` : t`Starting`;
    if (status.state === "running") return status.runnerReady === false ? t`Starting` : t`Running`;
    return t`Not started`;
  })();

  const harnessName = model ? (HARNESS_LABEL[model.harness] ?? model.harness) : null;
  const modelLabel = model
    ? model.model || (model.defaultModel ? t`Default · ${model.defaultModel}` : t`Default`)
    : null;

  return (
    <div className="space-y-5" data-testid="machine-settings">
      <SettingsGroup
        title={t`Machine`}
        footer={t`The machine your Conversation runs on. It starts when it is needed.`}
      >
        <SettingsRow label={t`Status`} value={stateLabel} />
      </SettingsGroup>

      <SettingsGroup title={t`Harness`}>
        <SettingsRow label={t`Harness`} value={harnessName ?? "-"} />
        <SettingsRow label={t`Model`} value={modelLabel ?? "-"} />
      </SettingsGroup>
    </div>
  );
}
