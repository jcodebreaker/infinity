import { ORPCError } from "@orpc/server";
import {
  deleteOmnigentModelConnection,
  getOmnigentBudget,
  getOmnigentModelPicks,
  getOmnigentMuse,
  getOmnigentSessionModel,
  listOmnigentModelConnections,
  listOmnigentModelProviders,
  listOmnigentModels,
  OmnigentApiError,
  type OmnigentBudget,
  type OmnigentModelCatalog,
  type OmnigentModelConnection,
  omnigentErrorCopy,
  putOmnigentBudget,
  putOmnigentModelConnection,
  putOmnigentModelPicks,
  putOmnigentMuseAgent,
  setOmnigentSessionModel,
} from "@superchat/adapters/omnigent/browser";
import type {
  EngineBudget,
  EngineHarnessId,
  EngineModelCatalog,
  EngineModelConnection,
} from "@superchat/contracts";
import { engineClient, museSessionId, NO_EMAIL } from "./client";
import { os } from "./context";

// The engine's model layer for the person (the engine owns models, keys and budgets): this page
// asks the engine, maps the answer to the screens' shapes and turns the engine's public error
// codes into typed errors the UI renders (`data.code`).

type OrpcCode =
  | "BAD_REQUEST"
  | "FORBIDDEN"
  | "NOT_FOUND"
  | "CONFLICT"
  | "PRECONDITION_FAILED"
  | "BAD_GATEWAY"
  | "SERVICE_UNAVAILABLE";

/** The RPC status of each engine code the model layer can answer with. */
const STATUS: Record<string, OrpcCode> = {
  model_key_required: "PRECONDITION_FAILED",
  model_not_supported: "BAD_REQUEST",
  model_budget_exhausted: "FORBIDDEN",
  model_provider_unreachable: "BAD_GATEWAY",
  account_suspended: "FORBIDDEN",
  forbidden: "FORBIDDEN",
  not_found: "NOT_FOUND",
  conflict: "CONFLICT",
  invalid_input: "BAD_REQUEST",
  superchat_not_configured: "SERVICE_UNAVAILABLE",
};

/** Runs an engine call; an engine error becomes an `ORPCError` carrying the engine's public
 * `code` in `data.code` and a message the person can read: `copy` for this call's own codes,
 * else the shared client copy. The engine's own text is passed on only for `invalid_input` (a
 * key the provider rejected), where it is written for the person and names no secret. */
async function onModels<T>(call: Promise<T>, copy: Record<string, string> = {}): Promise<T> {
  try {
    return await call;
  } catch (error) {
    if (!(error instanceof OmnigentApiError) || !error.code) throw error;
    const status = STATUS[error.code];
    if (!status) throw error;
    const message =
      copy[error.code] ??
      omnigentErrorCopy(error) ??
      (error.code === "invalid_input" ? error.detail : undefined) ??
      "That didn't work.";
    throw new ORPCError(status, { message, data: { code: error.code } });
  }
}

/** The built-in agents the Muse runs on, and the harness each runs. */
const AGENT_HARNESS: Record<string, { id: string; label: string }> = {
  "superchat-claude": { id: "claude-sdk", label: "Claude" },
  "superchat-pi": { id: "pi", label: "Pi" },
};
const ALL_HARNESSES = Object.values(AGENT_HARNESS);

/** The built-in agent that runs `harness`. */
function agentFor(harness: EngineHarnessId): string {
  const agent = Object.keys(AGENT_HARNESS).find((name) => AGENT_HARNESS[name]?.id === harness);
  if (!agent) throw new ORPCError("BAD_REQUEST", { message: "That didn't work." });
  return agent;
}

/** The harness this person's Muse runs on; both when its agent is not a built-in one. */
async function harnessesOf() {
  const muse = await getOmnigentMuse(engineClient(), NO_EMAIL);
  const known = AGENT_HARNESS[muse.agent];
  return { sessionId: muse.session_id, harnesses: known ? [known] : ALL_HARNESSES };
}

function toConnection(row: OmnigentModelConnection): EngineModelConnection {
  return {
    provider: row.provider,
    hint: row.hint,
    status: row.status,
    validatedAt: row.validated_at,
    label: row.label,
    scope: row.scope,
  };
}

function toCatalog(row: OmnigentModelCatalog): EngineModelCatalog {
  const picks = (raw: OmnigentModelCatalog["picks"] | undefined) => ({
    foundation: raw?.foundation ?? null,
    builder: raw?.builder ?? null,
    light: raw?.light ?? null,
  });
  return {
    harness: row.harness,
    status: row.status,
    providerLabel: row.provider_label,
    defaultModel: row.default_model,
    foundationModel: row.foundation_model ?? null,
    builderModel: row.builder_model ?? null,
    lightModel: row.light_model ?? null,
    picks: picks(row.picks),
    automatic: picks(row.automatic),
    models: row.models.map((model) => ({
      id: model.id,
      label: model.label,
      family: typeof model.family === "string" ? model.family : null,
      isDefault: model.is_default,
    })),
    error: row.error ?? null,
  };
}

function toBudget(row: OmnigentBudget): EngineBudget {
  return {
    monthlyLimitUsd: row.monthly_limit_usd,
    atLimit: row.at_limit,
    spentMonthUsd: row.spent_month_usd,
    orgLimitUsd: row.org_limit_usd ?? null,
  };
}

async function engineModelsStatus() {
  const { harnesses } = await harnessesOf();
  const catalogs = await Promise.all(
    harnesses.map((harness) =>
      listOmnigentModels(engineClient(), NO_EMAIL, harness.id).catch(() => undefined),
    ),
  );
  return {
    enabled: true,
    harnesses,
    ready: catalogs.some((catalog) => catalog?.status === "ready"),
  };
}

/** Whether the engine can serve a model for this person; `false` when it cannot answer. */
export async function modelsReady(): Promise<boolean> {
  try {
    return (await engineModelsStatus()).ready;
  } catch {
    return false;
  }
}

/** The person's Foundation pick from their model preferences: the harness their Muse runs on and
 * its chosen model; nulls when none is set or the engine cannot answer. */
export async function modelDefaults(): Promise<{ provider: string | null; model: string | null }> {
  try {
    const [{ harnesses }, picks] = await Promise.all([
      harnessesOf(),
      getOmnigentModelPicks(engineClient(), NO_EMAIL),
    ]);
    for (const harness of harnesses) {
      const model = picks.foundation[harness.id];
      if (model) return { provider: harness.id, model };
    }
  } catch {
    // The engine cannot answer: no default to show.
  }
  return { provider: null, model: null };
}

export const engineModelsRouter = {
  engineModels: {
    status: os.engineModels.status.handler(() => engineModelsStatus()),
    providers: os.engineModels.providers.handler(async () => {
      const rows = await onModels(listOmnigentModelProviders(engineClient(), NO_EMAIL));
      return rows.map((row) => ({
        provider: row.provider,
        label: row.label,
        operatorKey: row.operator_key,
      }));
    }),
    connections: os.engineModels.connections.handler(async () => {
      const rows = await onModels(listOmnigentModelConnections(engineClient(), NO_EMAIL));
      return rows.map(toConnection);
    }),
    connect: os.engineModels.connect.handler(async ({ input }) =>
      toConnection(
        await onModels(
          putOmnigentModelConnection(engineClient(), NO_EMAIL, input.provider, {
            api_key: input.apiKey,
            ...(input.label ? { label: input.label } : {}),
          }),
        ),
      ),
    ),
    disconnect: os.engineModels.disconnect.handler(async ({ input }) => {
      await onModels(deleteOmnigentModelConnection(engineClient(), NO_EMAIL, input.provider));
      return { ok: true as const };
    }),
    catalog: os.engineModels.catalog.handler(async ({ input }) =>
      toCatalog(await onModels(listOmnigentModels(engineClient(), NO_EMAIL, input.harness))),
    ),
    /** Moves the person's Superchat to the harness's agent; the engine refuses (`conflict`) while
     * a reply runs. */
    setHarness: os.engineModels.setHarness.handler(async ({ input }) => {
      await onModels(putOmnigentMuseAgent(engineClient(), NO_EMAIL, agentFor(input.harness)), {
        conflict: "Finish or stop the current reply first.",
      });
      return { ok: true as const };
    }),
    /** Sets one category's pick on one harness (`null`: back to automatic) and keeps the rest (the
     * engine replaces a category's whole map). */
    setPick: os.engineModels.setPick.handler(async ({ input }) => {
      const current = await onModels(getOmnigentModelPicks(engineClient(), NO_EMAIL));
      const { [input.harness]: _previous, ...others } = current[input.category];
      await onModels(
        putOmnigentModelPicks(engineClient(), NO_EMAIL, {
          [input.category]: input.model ? { ...others, [input.harness]: input.model } : others,
        }),
      );
      return { ok: true as const };
    }),
    budget: os.engineModels.budget.handler(async () =>
      toBudget(await onModels(getOmnigentBudget(engineClient(), NO_EMAIL))),
    ),
    setBudget: os.engineModels.setBudget.handler(async ({ input }) =>
      toBudget(
        await onModels(
          putOmnigentBudget(engineClient(), NO_EMAIL, {
            monthly_limit_usd: input.monthlyLimitUsd,
            at_limit: input.atLimit,
          }),
        ),
      ),
    ),
    /** The model the Muse's Conversation runs on (`null`: none stored) and the list to pick from,
     * for the harness it actually runs. */
    sessionModel: os.engineModels.sessionModel.handler(async () => {
      const { sessionId, harnesses } = await harnessesOf();
      const harness = harnesses[0]?.id ?? "";
      const [{ model, effectiveModel }, catalog] = await Promise.all([
        onModels(getOmnigentSessionModel(engineClient(), NO_EMAIL, sessionId)),
        onModels(listOmnigentModels(engineClient(), NO_EMAIL, harness)),
      ]);
      return { harness, model, effectiveModel, catalog: toCatalog(catalog) };
    }),
    setSessionModel: os.engineModels.setSessionModel.handler(async ({ input }) => {
      await onModels(
        setOmnigentSessionModel(engineClient(), NO_EMAIL, await museSessionId(), input.model),
      );
      return { ok: true as const };
    }),
  },
};
