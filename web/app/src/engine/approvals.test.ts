// @vitest-environment jsdom

import { call } from "@orpc/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { approvalsRouter } from "./approvals";
import { forgetMuseSession } from "./client";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

/** The engine as the page sees it; the Muse's Conversation is session `sess-1`. */
function stub(routes: Record<string, unknown>) {
  const fetchMock = vi.fn(async (url: URL | string, init?: RequestInit) => {
    const path = new URL(String(url)).pathname;
    const key = `${init?.method ?? "GET"} ${path}`;
    if (key === "GET /v1/me/muse") return json({ session_id: "sess-1" });
    if (!(key in routes)) throw new Error(`unexpected ${key}`);
    const value = routes[key];
    return value instanceof Response ? value : json(value);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const approval = (id: string, sessionId: string, extra = {}) => ({
  id: `approval:${id}`,
  kind: "approval",
  session_id: sessionId,
  objective_id: null,
  created_at: 1_700_000_300,
  subject: {
    category: "send",
    targets: ["bob@acme.test"],
    summary: "Send an email to bob@acme.test",
    amount_usd: null,
    can_always: true,
    always_label: "Send messages to bob@acme.test",
    ...extra,
  },
  choices: [
    { id: "approve_once", style: "primary" },
    ...("can_always" in extra && !extra.can_always
      ? []
      : [{ id: "approve_always", style: "secondary" }]),
    { id: "deny", style: "danger" },
  ],
});

const proposal = (id: string, isFirstPlan: boolean, sessionId = "sess-1") => ({
  id: `proposal:obj-1:${id}`,
  kind: "plan_proposal",
  session_id: sessionId,
  objective_id: "obj-1",
  created_at: 1_700_000_200,
  subject: {
    objective_title: "Plan the offsite",
    reason: "Add a review step",
    is_first_plan: isFirstPlan,
    plan: [
      { id: null, title: "Pick a venue" },
      { id: null, title: "Book it" },
    ],
  },
  choices: [
    { id: "accept", style: "primary" },
    { id: "dismiss", style: "secondary" },
  ],
});

const blocked = {
  id: "task:obj-1:t1",
  kind: "blocked_task",
  session_id: "sess-1",
  objective_id: "obj-1",
  created_at: 1_700_000_100,
  subject: {
    objective_title: "Plan the offsite",
    task_id: "t1",
    title: "Pick a venue",
    note: "Which city?",
  },
  choices: [{ id: "answer", style: "primary" }],
};

const session = (id: string, kind: string, root: string) => ({
  id,
  status: "idle",
  superchat: { kind, root_id: root, parent_id: root, project: null },
});

const listAsks = () => call(approvalsRouter.asks.list, { botId: "sess-1" });
const answerAsk = (askId: string, answer: string) =>
  call(approvalsRouter.asks.answer, { askId, runId: "r1", answer });

beforeEach(() => forgetMuseSession());
afterEach(() => vi.unstubAllGlobals());

describe("asks", () => {
  it("carries the call's arguments and the other policies' asks, never in the title", async () => {
    stub({
      "GET /v1/me/asks": {
        data: [
          approval("e2", "sess-1", {
            summary: "Over budget. Continue? (web_search)",
            can_always: false,
            arguments: '{"query": "q3"}',
            also_asks: ["Pushes need a look."],
          }),
        ],
      },
    });
    const [ask] = await listAsks();
    expect(ask).toMatchObject({
      text: "Over budget. Continue? (web_search)",
      approval: { arguments: '{"query": "q3"}', alsoAsks: ["Pushes need a look."] },
    });
  });

  it("maps the engine's kinds and choice ids to Superchat's copy", async () => {
    stub({
      "GET /v1/me/asks": {
        data: [
          approval("e1", "sess-1"),
          proposal("p-first", true),
          proposal("p-change", false),
          blocked,
        ],
      },
    });
    const asks = await listAsks();
    expect(asks.map((ask) => [ask.kind, ask.id])).toEqual([
      ["approval", "approval:e1"],
      ["proposal", "proposal:obj-1:p-first"],
      ["proposal", "proposal:obj-1:p-change"],
      ["blocked_task", "task:obj-1:t1"],
    ]);
    expect(asks[0]).toMatchObject({
      runId: "sess-1",
      text: "Send an email to bob@acme.test",
      detail: "Always allow: Send messages to bob@acme.test",
      choices: [
        { id: "approve_once", label: "Allow once" },
        { id: "approve_always", label: "Always allow" },
        { id: "deny", label: "Deny" },
      ],
    });
    expect(asks[1]).toMatchObject({
      runId: "obj-1",
      goalId: "obj-1",
      goalTitle: "Plan the offsite",
      text: 'Here\'s my plan for "Plan the offsite"',
      detail: "1. Pick a venue\n2. Book it",
      choices: [
        { id: "accept", label: "Start this plan" },
        { id: "dismiss", label: "Not now" },
      ],
    });
    expect(asks[2]).toMatchObject({
      text: 'Change the plan for "Plan the offsite"?',
      detail: "Add a review step\n1. Pick a venue\n2. Book it",
      choices: [
        { id: "accept", label: "Use the new plan" },
        { id: "dismiss", label: "Keep current" },
      ],
    });
    expect(asks[3]).toMatchObject({ text: "Which city?", input: "text", choices: [] });
  });

  it("leaves out Always allow when the engine does", async () => {
    stub({ "GET /v1/me/asks": { data: [approval("e1", "sess-1", { can_always: false })] } });
    const [ask] = await listAsks();
    expect(ask?.choices.map((choice) => choice.id)).toEqual(["approve_once", "deny"]);
    expect(ask?.detail).toBeUndefined();
  });

  it("keeps only the asks of this Muse's Conversation and its Helpers", async () => {
    stub({
      "GET /v1/me/asks": {
        data: [
          approval("helper", "sess-helper"),
          approval("other", "sess-other"),
          proposal("p-other", true, "sess-2"),
        ],
      },
      "GET /v1/sessions/sess-helper": session("sess-helper", "helper", "sess-1"),
      "GET /v1/sessions/sess-other": session("sess-other", "helper", "sess-2"),
    });
    const asks = await listAsks();
    expect(asks.map((ask) => ask.id)).toEqual(["approval:helper"]);
  });

  it("counts what is waiting", async () => {
    stub({ "GET /v1/me/asks": { data: [approval("e1", "sess-1"), blocked] } });
    await expect(call(approvalsRouter.asks.count, { botId: "sess-1" })).resolves.toEqual({
      count: 2,
    });
  });

  it("answers with the choice id, and a blocked Task with the reply as its note", async () => {
    const fetchMock = stub({
      "POST /v1/me/asks/approval%3Ae1/answer": { ok: true },
      "POST /v1/me/asks/task%3Aobj-1%3At1/answer": { ok: true },
    });
    await answerAsk("approval:e1", "approve_always");
    await answerAsk("task:obj-1:t1", "Toronto");
    const bodies = fetchMock.mock.calls
      .filter(([, init]) => init?.method === "POST")
      .map(([, init]) => JSON.parse(String(init?.body)));
    expect(bodies).toEqual([{ choice: "approve_always" }, { choice: "answer", note: "Toronto" }]);
  });

  it("reports an ask that is no longer waiting as a conflict", async () => {
    stub({
      "POST /v1/me/asks/proposal%3Aobj-1%3Ap/answer": json(
        { error: { code: "conflict", message: "proposal is no longer open" } },
        409,
      ),
    });
    await expect(answerAsk("proposal:obj-1:p", "accept")).rejects.toMatchObject({
      code: "CONFLICT",
      message: "This prompt is no longer awaiting an answer",
    });
  });

  it("rejects an id the engine did not give", async () => {
    stub({});
    await expect(answerAsk("nope", "x")).rejects.toMatchObject({ code: "BAD_REQUEST" });
  });
});

describe("approvals", () => {
  it("lists rules with the spending cap, revokes a rule and sets the cap", async () => {
    stub({
      "GET /v1/me/approval-rules": {
        rules: [
          {
            id: "r1",
            category: "send",
            target: "*@acme.test",
            label: "Send messages to *@acme.test",
            decision: "allow",
            created_at: 5,
          },
        ],
      },
      "GET /v1/me/approval-settings": { daily_cap_usd: 0, spent_today_usd: 0 },
      "DELETE /v1/me/approval-rules/r1": new Response(null, { status: 204 }),
      "PUT /v1/me/approval-settings": { daily_cap_usd: 25, spent_today_usd: 0 },
    });
    expect(await call(approvalsRouter.approvals.rules, { botId: "sess-1" })).toEqual({
      rules: [{ id: "r1", label: "Send messages to *@acme.test", decision: "allow", createdAt: 5 }],
      spending: { dailyCapUsd: 0, spentTodayUsd: 0 },
    });
    await expect(
      call(approvalsRouter.approvals.revoke, { botId: "sess-1", ruleId: "r1" }),
    ).resolves.toEqual({ ok: true });
    await expect(
      call(approvalsRouter.approvals.setSpending, { botId: "sess-1", dailyCapUsd: 25 }),
    ).resolves.toEqual({ dailyCapUsd: 25, spentTodayUsd: 0 });
  });
});
