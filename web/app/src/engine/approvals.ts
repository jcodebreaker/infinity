// Asks (ADR 0009): the engine's decisions inbox (`GET /v1/me/asks`) is the one list of what waits
// on the person (approvals, plan proposals, blocked Tasks), and `POST /v1/me/asks/{id}/answer`
// answers any of them. The engine sends kinds and choice ids; the wording below is Superchat's.
// The standing rules and daily spending cap Settings shows are the engine's too.
import { ORPCError } from "@orpc/server";
import {
  answerOmnigentAsk,
  createChatOwnershipResolver,
  deleteOmnigentApprovalRule,
  getOmnigentApprovalSettings,
  listOmnigentApprovalRules,
  listOmnigentAskHistory,
  listOmnigentAsks,
  OmnigentApiError,
  type OmnigentAsk,
  putOmnigentApprovalSettings,
} from "@superchat/adapters/omnigent/browser";
import type { Ask } from "@superchat/contracts";
import { engineClient, museSessionId, NO_EMAIL } from "./client";
import { onSuperChat, os } from "./context";

const iso = (epochSeconds: number) => new Date(epochSeconds * 1000).toISOString();

/** Ids the engine gives its asks: `approval:…`, `proposal:…:…` or `task:…:…`. */
function isEngineAskId(askId: string): boolean {
  return /^(approval|proposal|task):/.test(askId);
}

const APPROVAL_CHOICES: Record<string, string> = {
  approve_once: "Allow once",
  approve_always: "Always allow",
  deny: "Deny",
};

function proposalChoices(isFirstPlan: boolean): Record<string, string> {
  return isFirstPlan
    ? { accept: "Start this plan", dismiss: "Not now" }
    : { accept: "Use the new plan", dismiss: "Keep current" };
}

function labelled(ask: OmnigentAsk, labels: Record<string, string>): Ask["choices"] {
  return ask.choices.flatMap((choice) => {
    const label = labels[choice.id];
    return label ? [{ id: choice.id, label }] : [];
  });
}

/** An engine ask as the Ask the Feed, the waiting list and the Conversation show. */
function toAsk(ask: OmnigentAsk): Ask {
  const createdAt = iso(ask.created_at);
  if (ask.kind === "approval") {
    const { subject } = ask;
    return {
      id: ask.id,
      runId: ask.session_id,
      kind: "approval",
      goalId: null,
      goalTitle: null,
      text: subject.summary,
      detail: subject.can_always ? `Always allow: ${subject.always_label}` : undefined,
      choices: labelled(ask, APPROVAL_CHOICES),
      input: null,
      createdAt,
      approval: {
        arguments: subject.arguments ?? null,
        alsoAsks: subject.also_asks ?? [],
      },
    };
  }
  const goalId = ask.objective_id ?? ask.session_id;
  if (ask.kind === "plan_proposal") {
    const { subject } = ask;
    const steps = subject.plan.map((item, index) => `${index + 1}. ${item.title}`);
    return {
      id: ask.id,
      runId: goalId,
      kind: "proposal",
      goalId,
      goalTitle: subject.objective_title,
      text: subject.is_first_plan
        ? `Here's my plan for "${subject.objective_title}"`
        : `Change the plan for "${subject.objective_title}"?`,
      detail: (subject.is_first_plan ? steps : [subject.reason, ...steps]).join("\n"),
      choices: labelled(ask, proposalChoices(subject.is_first_plan)),
      input: null,
      createdAt,
    };
  }
  return {
    id: ask.id,
    runId: goalId,
    kind: "blocked_task",
    goalId,
    goalTitle: ask.subject.objective_title,
    text: ask.subject.note?.trim() || ask.subject.title,
    // The person answers in words: the reply goes into the Task's note.
    choices: [],
    input: "text",
    createdAt,
  };
}

/** The asks of the Muse: its Goals' (on its Conversation) and the approvals held in its
 * Conversation and Helpers. Newest first, as the engine orders them. */
async function listAsks(): Promise<Ask[]> {
  const superSessionId = await museSessionId();
  const waiting = await onSuperChat(listOmnigentAsks(engineClient(), NO_EMAIL));
  const ownershipOf = createChatOwnershipResolver(engineClient(), NO_EMAIL, [
    { botId: superSessionId, omnigentSessionId: superSessionId },
  ]);
  const asks: Ask[] = [];
  for (const ask of waiting) {
    if (ask.session_id === superSessionId) {
      asks.push(toAsk(ask));
    } else if (ask.kind === "approval") {
      const owner = await ownershipOf(ask.session_id);
      if (owner) asks.push(toAsk(ask));
    }
  }
  return asks;
}

/** `approvals.*` (the engine's standing rules and spending cap) and `asks.*` (the decisions
 * inbox: approvals, plan proposals and blocked Goal tasks). */
export const approvalsRouter = {
  approvals: {
    rules: os.approvals.rules.handler(async () => {
      const [rules, settings] = await onSuperChat(
        Promise.all([
          listOmnigentApprovalRules(engineClient(), NO_EMAIL),
          getOmnigentApprovalSettings(engineClient(), NO_EMAIL),
        ]),
      );
      return {
        rules: rules.map((rule) => ({
          id: rule.id,
          label: rule.label,
          decision: rule.decision,
          createdAt: rule.created_at,
        })),
        spending: { dailyCapUsd: settings.daily_cap_usd, spentTodayUsd: settings.spent_today_usd },
      };
    }),
    revoke: os.approvals.revoke.handler(async ({ input }) => {
      await onSuperChat(deleteOmnigentApprovalRule(engineClient(), NO_EMAIL, input.ruleId));
      return { ok: true as const };
    }),
    setSpending: os.approvals.setSpending.handler(async ({ input }) => {
      const settings = await onSuperChat(
        putOmnigentApprovalSettings(engineClient(), NO_EMAIL, input.dailyCapUsd),
      );
      return { dailyCapUsd: settings.daily_cap_usd, spentTodayUsd: settings.spent_today_usd };
    }),
  },
  asks: {
    list: os.asks.list.handler(() => listAsks()),
    history: os.asks.history.handler(async () =>
      (await onSuperChat(listOmnigentAskHistory(engineClient(), NO_EMAIL))).map((row) => ({
        id: row.id,
        kind: row.kind,
        title: row.title,
        decision: row.decision,
        byYou: row.by_you,
        decidedBy: row.decided_by,
        decidedAt: iso(row.decided_at),
      })),
    ),
    count: os.asks.count.handler(async () => ({ count: (await listAsks()).length })),
    /** Answers an engine ask with one of its choice ids; a blocked Task's `answer` is the
     * person's reply. The engine checks the ask is theirs. */
    answer: os.asks.answer.handler(async ({ input }) => {
      if (!isEngineAskId(input.askId)) {
        throw new ORPCError("BAD_REQUEST", { message: "This is not an Ask the engine is holding" });
      }
      const answer = input.askId.startsWith("task:")
        ? { choice: "answer", note: input.answer }
        : { choice: input.answer };
      try {
        await answerOmnigentAsk(engineClient(), NO_EMAIL, input.askId, answer);
      } catch (error) {
        if (error instanceof OmnigentApiError) {
          if (error.code === "not_found" || error.code === "conflict") {
            throw new ORPCError("CONFLICT", {
              message: "This prompt is no longer awaiting an answer",
            });
          }
          if (error.code === "invalid_input") {
            throw new ORPCError("BAD_REQUEST", { message: "Could not submit this answer" });
          }
        }
        throw error;
      }
      return { ok: true as const };
    }),
  },
};
