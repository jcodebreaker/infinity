import { Trans, useLingui } from "@lingui/react/macro";
import type { EngineModelCatalog } from "@superchat/contracts";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@superchat/ui-web";
import { Cpu } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { errorText } from "../../lib/error-text";
import { rpc } from "../../lib/rpc";
import { foundationOf, modelOptionLabel } from "./DefaultModel";
import { useEngineModelsStatus } from "./engine-models";

const DEFAULT_VALUE = "__default__";

/**
 * The model this Conversation runs on, in the toolbar: a quiet button named after the model the
 * engine runs it on, marked "pinned" while the chat stores a model of its own. It
 * opens the list the engine serves for the Conversation's harness; "Default" returns to the
 * Foundation. Absent when Superchat runs without the engine; asked again whenever the person's
 * models change (Settings > Models).
 */
export function ConversationModelPicker({ botId }: { botId: string }) {
  const { t } = useLingui();
  const [status] = useEngineModelsStatus();
  const [state, setState] = useState<{
    model: string | null;
    effectiveModel: string | null;
    catalog: EngineModelCatalog;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const enabled = status?.enabled === true;

  const load = useCallback(async () => {
    setError(null);
    try {
      const next = await rpc.engineModels.sessionModel({ botId });
      setState({ model: next.model, effectiveModel: next.effectiveModel, catalog: next.catalog });
    } catch (err) {
      setError(errorText(err, t`Could not load models.`));
    }
  }, [botId, t]);

  // `status` is a new answer after any change to the person's models: read the chat's again.
  useEffect(() => {
    if (enabled && status) void load();
  }, [enabled, status, load]);

  if (!enabled) return null;

  async function choose(value: string) {
    if (!state) return;
    const model = value === DEFAULT_VALUE ? null : value;
    const before = state;
    setState({ ...state, model, effectiveModel: model ?? foundationOf(state.catalog) });
    setError(null);
    try {
      await rpc.engineModels.setSessionModel({ botId, model });
      // The engine says which model the chat now runs on.
      await load();
    } catch (err) {
      setState(before);
      setError(errorText(err, t`Could not switch model.`));
    }
  }

  const catalog = state?.catalog;
  const usable = catalog?.status === "ready" && catalog.models.length > 0;
  const foundation = catalog ? foundationOf(catalog) : null;
  const labelOf = (id: string | null) => {
    const row = catalog?.models.find((m) => m.id === id);
    return row ? modelOptionLabel(row) : null;
  };
  // The engine's answer; with none the harness runs the connection's default.
  const inUse = state ? (state.effectiveModel ?? catalog?.defaultModel ?? null) : null;
  const inUseLabel = usable ? labelOf(inUse) : null;
  const defaultLabel = labelOf(foundation);
  // Pinned: the chat stores a model of its own (Default stores none).
  const pinned = usable && state?.model != null;
  return (
    <DropdownMenu onOpenChange={(open) => open && void load()}>
      <DropdownMenuTrigger
        data-testid="conversation-model-trigger"
        title={t`Model`}
        className="sc-glass app-no-drag inline-flex h-11 items-center gap-2 rounded-xl px-3.5 text-[14px] font-medium text-foreground transition-colors hover:bg-foreground/10 focus-visible:outline-2 focus-visible:outline-ring data-[popup-open]:bg-foreground/10"
      >
        <Cpu size={18} strokeWidth={1.7} aria-hidden="true" />
        {inUseLabel ?? <Trans>Model</Trans>}
        {pinned ? (
          <span data-testid="conversation-model-pinned" className="text-[12.5px] text-ink-3">
            <Trans>pinned</Trans>
          </span>
        ) : null}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" data-testid="conversation-model-menu" className="min-w-52">
        <DropdownMenuGroup>
          <DropdownMenuLabel>
            <Trans>Model for this chat</Trans>
          </DropdownMenuLabel>
        </DropdownMenuGroup>
        {usable && state ? (
          <DropdownMenuRadioGroup
            value={pinned ? (state.model ?? DEFAULT_VALUE) : DEFAULT_VALUE}
            onValueChange={choose}
          >
            <DropdownMenuRadioItem value={DEFAULT_VALUE}>
              {defaultLabel ? t`Default · ${defaultLabel}` : t`Default`}
            </DropdownMenuRadioItem>
            <DropdownMenuSeparator />
            {state.catalog.models.map((model) => (
              <DropdownMenuRadioItem key={model.id} value={model.id}>
                {modelOptionLabel(model)}
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        ) : state ? (
          <p className="px-2 py-1.5 text-[12.5px] text-ink-3">
            <Trans>Add a key in Settings to choose a model.</Trans>
          </p>
        ) : null}
        {error ? (
          <p role="alert" className="px-2 py-1.5 text-[12.5px] text-destructive">
            {error}
          </p>
        ) : null}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
