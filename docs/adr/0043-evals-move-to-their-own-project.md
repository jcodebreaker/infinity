# 0043: Evals move to their own project; Superchat keeps target support

> **Status:** Accepted 2026-11-01 by the owner ("evals should be it own kinda project that we can wire to any project that have phoenix traces"). Amends [0041](0041-evals-are-cases-in-the-repo-and-runs-are-traces.md) and [0042](0042-an-evals-agent-runs-the-pipeline-in-isolated-sessions.md).

0041 and 0042 built the eval platform inside the engine: cases, runs, grading, the run analysis, the evals agent and the admin page. Most of it never needed Superchat. Matching, grading, analysis, the agent and the page only need a way to play a conversation and the traces of what happened. Kept here, only Superchat can be evaluated, and every eval change ships with the engine.

## Decision

**Evals move to evalution, a separate private service.** It holds the cases, the runs, the agent and the admin page, and tests any app (a **target**) through an adapter plus the app's Phoenix project. Its superchat adapter plays through Superchat's public API as a fresh eval person per run, and reads the trajectory, what each model call was sent, and tokens and cost from Phoenix spans.

**Superchat keeps only target support:**

- **Eval identities and their exclusions**, unchanged from 0042: `eval+<run>@<eval domain>`, left out of the people list, the Lookout, the daily brief, proactive messages and the quiet-moment passes.
- **A registry of eval persons.** One `eval_person` row per person in the generic `preferences` table, `{created_at, released_at}` under their user id. It is written the first time an eval identity asks for its Conversation (`GET /v1/me/muse`) and on release. Deleting the person deletes the row.
- **`POST /v1/eval-persons/{email}/release`** (admin). It refuses anyone who is not a run's eval person. Otherwise it stops their running turns, removes their host through the host provider (the home is kept), pauses their scheduled tasks and marks them released.
- **The 7-day sweep reads the registry.** Its candidates are only registered eval persons, never an address that merely looks like one. A person is deleted, with host, home and every row, once their newest activity (registered, released, last session update) is more than 7 days old. Before, the candidates came from the built-in runner's run files.
- **LLM spans** from the model-call proxy (`harnesses/llm_trace.py`), with `session.id` set to the Conversation. With content capture on, they carry what the model was sent; evalution splits that request into context sections using the engine's markers.

**The built-in runner, routes and `/evals` page stay until evalution is verified against this stack, then go.** Until then they are deprecated: nothing new is added to them, and their eval persons use the same registry and sweep.

## Considered options

- **Keep evals here, behind a plug-in seam.** Every other app would have to run this engine to be evaluated.
- **Find eval persons by email pattern in the sweep.** Plus-addressing on the eval domain would make a real account a deletion candidate. Only a registry row Superchat wrote itself makes someone a candidate.
- **A dedicated table.** It needs a migration for three fields. The `preferences` table already keys rows by user and is already wiped by the person delete, as the suspension flag is.

## Consequences

- Eval persons made before the registry existed are never swept. An admin removes them with `DELETE /v1/admin/users/{id}`.
- Without content capture (`OMNIGENT_OTEL_CAPTURE_CONTENT`), evalution sees each model call's tokens but not its sections.
- Helpers' model calls carry their own session id on spans. evalution finds them through the trace, not through `session.id`.

## Amendment 2026-11-01

The built-in runner and page were removed on 2026-11-01 at the owner's request ("remove the current eval under superchat"), without waiting for evalution to be verified against this stack. Gone: the cases, matching, grading, analysis, drafting, playing, result store, evals agent and `/v1/evals/*` routes in the engine, `engine/scripts/evals.py`, the repo's `evals/` sets, the `/evals` page with its contract and adapter, and the `OMNIGENT_EVALS_*` settings. The target support above stays ([evals.md](../evals.md)).
