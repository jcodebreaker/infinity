# Evals

Evals live in evalution, a separate service that tests any app through an adapter and the app's Phoenix traces ([ADR 0043](adr/0043-evals-move-to-their-own-project.md)). Cases, runs, grading, analysis, the evals agent and the admin page are all there. Superchat is one of its targets and provides only what follows.

## Eval persons

- **The identity.** Each run plays as its own person, `eval+<run id>@<eval domain>`, through Superchat's public API. The domain is that of `SUPERCHAT_EVAL_EMAIL` (default `evals@superchat.local`); that address itself is the shared eval person. In header and OIDC modes the email is the user id, so `is_eval_identity(user_id)` (`engine/omnigent/evals/identity.py`) needs no table.
- **Exclusions.** Eval persons are left out of the admin people list, the Lookout (their signals are dropped, so it never wakes), the daily brief, the outbox (no proactive messages) and the quiet-moment passes (no daily notes, topics, Dreaming or People passes). Memory upkeep still runs for them.
- **The registry** (`engine/omnigent/evals/people.py`). One `eval_person` row per run's person in the `preferences` table, `{created_at, released_at}` under their user id. It is written the first time the person calls `GET /v1/me/muse` and on release. Deleting the person deletes the row.
- **`POST /v1/eval-persons/{email}/release`** (admin). It refuses anyone who is not a run's eval person. Otherwise it stops their running turns, removes their host through the host provider (the home is kept), pauses their scheduled tasks and marks them released. evalution calls it at the end of each run.
- **The 7-day sweep.** A daily job of the admin feature deletes every registered eval person whose newest activity (registered, released, last session update) is more than 7 days old: the host with its home, then everything the admin person delete removes. Its candidates come only from the registry, never from the email pattern alone, and it logs each removal.

## Traces

- **LLM spans.** With `OTEL_EXPORTER_OTLP_ENDPOINT` set, the model-call proxy (`engine/omnigent/harnesses/llm_trace.py`) emits one LLM span per model call (model, tokens, cost) under the running turn, with `session.id` set to the Conversation. With `OMNIGENT_OTEL_CAPTURE_CONTENT` on, the span also carries what the model was sent and its answer. Each Round is one trace, and a Conversation's `omnigent.eval.*` labels are copied onto its Round span.
- **Context records.** An eval person's host runners get `OMNIGENT_LLM_CONTEXT_DIR=.omnigent/llm-context`, so the proxy also appends one record per model call to `<workspace>/.omnigent/llm-context/<conversation id>.jsonl`. evalution's reset of a run's person cleans them.
- **The observability profile.** Compose's `observability` profile starts Phoenix on `127.0.0.1:6006`. Point the engine at it with `OTEL_EXPORTER_OTLP_ENDPOINT=http://host.docker.internal:6006`, `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`, `OTEL_METRICS_EXPORTER=none` and `OTEL_LOGS_EXPORTER=none`.
