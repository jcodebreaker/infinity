# 0041: Evals are cases in the repo, and runs are traces

> **Status:** Accepted 2026-10-31 by the owner, who approved the mock-up ("ready to implement") and took the defaults: the page lives in the web app, Evolve v1 changes only the Assistant's prompt, and this branches from `lookout-wave`.

Nothing today can say "for this ask, the Assistant should call these tools, in this shape, and answer like this", and then show whether it did. `engine/scripts/scenarios.py` plays the ADR 0025 scenarios and ADR 0029's Round matrix. It checks that a turn does not stall or stay silent, that replies don't name the machinery, and that the waits stay in budget. It never checks which tools were called or whether the reply was good. The graded script on `wave4` was built for ADR 0028 (plans, Helper order). It never merged, and its graders test words that ADR 0029 retired.

The engine already emits OpenTelemetry spans in OpenInference names: AGENT, TOOL and GUARDRAIL, with `input.value`, `output.value` and `session.id` (`harnesses/tracing.py`). It exports them over OTLP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set (`runtime/telemetry.py`), and the trace context reaches the runners. Model calls made inside a harness CLI are not spans. The `llm_trace` proxy writes them to JSONL, and only in dev.

## Decision

**A case is a file in the repo.** Each case is one JSON file under `evals/sets/<set>/` and holds:

- **Turns:** the person's messages, with any fixture files.
- **Expected tool calls:** argument matchers, an order mode, parallel groups and a never-call list.
- **A good reply:** a reference answer plus rubric lines.
- **Limits:** tries, time, cost and pass scores.

The shape follows Google ADK's eval cases (turns, expected tool use, final response), so ADK eval sets can be imported. Our extras are listed in [evals.md](../evals.md). Cases are reviewed in pull requests like code.

**A run plays every case several times on the live stack.** Each try plays in an eval Conversation of its own, never the person's. It reads what happened from the stored Round (its items and the Round view), and it grades two things:

- **Tool calls**, by code. This needs no model.
- **The reply**, by one model call per try against the rubric. The **Grader** model is pinned per set, so changing it starts a new baseline.

A try passes when both scores clear the set's pass scores and no case limit is broken. Over its tries a case is **pass** (every try passed), **flaky** (some failed, fewer than half) or **fail** (half or more failed). Time, tokens, cost and the number of tool and model calls are recorded on every try, but they never decide a verdict unless the case sets a limit.

**Every try is one trace.** The Round is the root, with its turns, every tool call, every Helper it started and every model call under it. Two gaps close for this:

- The `llm_trace` proxy also emits an OpenInference LLM span (model, messages, tool calls, tokens, cost) under the current span.
- Wake turns and Helpers join their Round's trace.

Eval tries tag their spans `eval.set`, `eval.case`, `eval.run` and `eval.try`.

**Phoenix shows the traces and can hold the results.** Phoenix is self-hosted, runs as one compose service under the `observability` profile, and is off by default. The engine exports to it only when the endpoint is set. Prompts and answers in spans (content capture) stay dev- and admin-only, as today.

Results are written through one small store interface with two adapters:

- **Files:** each run as JSON under the engine's data directory. This is the default, and it is what the offline tests use.
- **Phoenix:** each set is a dataset, each run an experiment, each try a repetition, and each grade an annotation with its explanation.

The product never needs Phoenix to run evals. Phoenix adds the trace view and its own compare screens.

**The page lives in the web app** at `/evals`, for admins only. It talks to a generic engine API, `/v1/evals/*`, with nothing Superchat-specific in it. Its screens are the approved mock-up:

- **Run:** the numbers compared with an earlier run, the cases with their score bars, and a 10-run history.
- **Case:** the ask beside what was expected, the tries as side-by-side cards showing each one's conversation with tool chips marked against what was expected, the timeline of the chosen try, and a step inspector.
- **Prompt version:** v7 against v8, the diff, and the cases that changed.
- **New case:** made from a test chat by tapping each tool to expected, any or never.

Every run starts behind a confirm that shows the estimate and the pass scores.

**Evolve v1 changes only the Assistant's prompt**, `engine/agents/templates/AGENTS.md` (rendered into both bundles). A model reads the failed tries and their grades and proposes a diff with its reasons, and the candidate runs on the same set. It is offered only with zero regressions and at least one case better. The owner approves on the page, and approval writes a commit on a branch. Each Evolve session has a cost cap and asks before every run. Skills come later.

## Considered options

- **Extend `scenarios.py` rules only.** This keeps the wait budgets, but the rules cannot say which tools a given ask should use or whether the answer was right.
- **A hosted eval product** (LangSmith, Braintrust, Arize AX). This breaks "no hosted vendor is required", and sends prompts and answers off the machine.
- **Our own Postgres tables for runs.** This is more code for what Phoenix already models, and the file store covers the offline case. Reconsider it if Phoenix's REST API cannot serve the page.
- **Run ADK's evaluator as a library.** It brings a Python ADK dependency, and its trajectory assumes an ADK agent. We copy the case shape, not the library.

## Consequences

- **Engine:** a capability folder `engine/omnigent/evals/` (case loading, matching, grading, playing a try, the result store and its two adapters), and the `/v1/evals/*` routes. The `llm_trace` proxy emits LLM spans. The Round's trace covers its wake turns and Helpers.
- **Deployment:** Phoenix as a compose service under the `observability` profile, with the endpoint reaching the runners through the existing env allowlist.
- **Web:** a feature folder `web/app/src/features/evals/` behind the admin check.
- **Cases:** `evals/sets/core/` starts from ADR 0029's Round matrix with expected tool calls added, then grows from test chats.
- **Spend:** every graded run spends real model calls. The script and the page both show the estimate and ask, the script unless `--yes`.
- **Unchanged:** `scenarios.py` keeps the wait budgets and the stall and silence rules.
