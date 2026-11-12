# Engine

The engine is one Python package, `omnigent`, and one process. It serves the API and the built web,
owns Postgres, sessions and scheduling, and starts each person's host (a Docker container
today), where a runner runs one harness per session. The same package is installed in the host;
the host only uses its HOST half. The layers and their rules are decided in
[ADR 0019](../docs/adr/0019-repository-layout-and-layers.md); the words (engine, host, runner,
harness, helper) in [ADR 0018](../docs/adr/0018-one-vocabulary-engine-host-runner-harness.md).

## Layer map

```
engine/
├── omnigent/
│   ├── server/                       ENGINE   API, sessions, scheduler, auth
│   ├── superchat/                    PRODUCT  features, tools, prompts, memory, artifacts, decks, helpers
│   ├── db/  stores/                  DATA     Postgres models, migrations, stores
│   ├── host/  runner/  harnesses/    HOST     runs inside a person's host
│   └── spec/ runtime/ llms/ tools/ policies/ entities/ context/ models/ util/   SHARED
├── agents/     the system prompt and helper types: templates, the render script, the rendered bundles
├── tests/
└── README.md   the layer map and the where-is table
```

Folders not named in the tree (`hosts/`, `onboarding/`, `memory/`, `sandbox/`, `terminals/`, `telemetry/`,
`connections/`, `extensions/`, `api/`, `resources/` and the top-level modules) are SHARED until a
later wave places them.

Two import-linter contracts in `pyproject.toml` hold the layers: HOST does not import `server`, and
only `server` and the runner import `superchat`. Run them with `lint-imports` from `engine/`; CI
runs them too. They check direct imports, and both pass. When a lower layer needs something from
a higher one, move the shape down (as `runtime/runner_wire.py` and `entities/session_project.py`
did) or let the higher layer fill a registry: `superchat/features.py` is the product's composition
root, and importing it registers Superchat's tools with `tools/manager.py`
(`register_tool_source`) and its policies and owner budgets with `runtime/policies/hooks.py`.

## Where is

| What | Where |
|---|---|
| The system prompt | Edited in `agents/templates/` (`AGENTS.md`, `filesystem.md`, `decks.md`, `config.yaml.tmpl`). `node engine/agents/render-agents.mjs` writes the bundles to `agents/bundles/superchat-claude` and `agents/bundles/superchat-pi`. The engine image renders its own copy at build (the `agents` stage of `deployment/engine.Dockerfile`); the committed copy stays for the tests, and `tests/superchat/test_muse_harness_bundles.py` fails when it drifts from a fresh render. |
| The orchestrator (the assistant's turn) | The runner runs the turn: `runner/app.py` (turn start, message prefixes, wake delivery) and `runner/tool_dispatch.py` (`dispatch_tool_locally`, which hands Superchat tools to their feature). The features and their handlers are registered once in `superchat/features.py`; each feature is a folder under `superchat/` with a `FEATURE`. The turn prefix is built in `superchat/prompt_prefix.py`. |
| Helpers | `start_helper` and its siblings: `superchat/helpers/` (`tools.py`, `handlers.py`). Sub-agent sessions, nesting rules and the wake notice text: `superchat/subagents.py`. The wake notice is posted back to the parent by `runner/app.py` (`_deliver_subagent_wake_post`). Helper types are the `agents/<type>/config.yaml` entries of each bundle. |
| Tools | Engine built-ins: `tools/builtins/`; the tool manager: `tools/manager.py`. Superchat tools: each feature's `superchat/<feature>/tools.py` and `handlers.py`, registered with the tool manager by `superchat/features.py`. |
| Policies | The builder and engine: `runtime/policies/` (`builder.py`, `engine.py`); the hooks a product fills: `runtime/policies/hooks.py`. Superchat's approvals: `superchat/approvals/policy.py`. |
| Runner wire shapes | `runtime/runner_wire.py` (background titles, `session.created`, the SSE event base); `server/schemas.py` re-exports them. |
| Smart routing | `server/smart_routing.py` and `server/routing_backend.py`; native-subagent routing: the runner's relay in `runner/subagent_routing.py`, the server policy in `server/subagent_route_policy.py`. |
| Memory and recall | Long-term memory: `memory/` (service, index, upkeep) and `superchat/memory/` (the `memory_*` tools and the profile). Recall: `superchat/recall_hint/` (the bounded hint and its planner) and `superchat/knowledge/recall.py`. File search: `superchat/knowledge/` (engine relays plus the index that runs in the host). |
| Artifacts and decks | `superchat/artifacts/` (store, routes, patches, write-back) and `superchat/decks/` (the deck kit under `decks/kit/`, authoring, look). Live pages: `superchat/live/`. Sheets: `superchat/sheets/`. |
| Background runs (work queue) | `server/work_queue/` (ADR 0038): one `work_jobs` table every background model run waits in (`submit`, `register_handler`), with the global and per-owner limits, the worker and its reaper. Its jobs: scheduled Helper fires (`server/scheduled/helper_fire.py`, kind `scheduled_fire`), quiet-moment passes (`superchat/daily_notes/quiet_moment.py`) and memory upkeep (`server/memory_upkeep.py`). |
| Postgres | Models: `db/db_models.py`. Migrations: `db/migrations/` (Alembic, `db/alembic.ini`). Stores: `stores/` (one folder per store) and `superchat/*/store.py` for feature stores. |
| The host provider | `hosts/docker.py` (the `docker` provider; the launcher contract in `hosts/launcher.py`, the provider registry in `hosts/registry.py`) launches the host container; `server/hosts.py` parses the `hosts:` config block and tracks the hosts the engine started. |
| The two harnesses | `harnesses/claude_sdk_executor.py` (Claude Agent SDK) and `harnesses/pi_executor.py` (Pi), with their harness wrappers `harnesses/claude_sdk_harness.py` and `harnesses/pi_harness.py`. |
| The host daemon | `host/` (connect, runner zygote, skills) and the `omnigent host` command in `cli.py`. It runs in every host and starts the runners (`runner/_entry.py`). |
| The engine entry point | `deployment/entrypoint.py` (config, migrations, app), started by `deployment/engine/start.sh` in the engine image. |
| Config templates | `deployment/engine/config.yaml.tmpl`, `config.litellm.yaml.tmpl` and `config.byok.yaml.tmpl`; `start.sh` renders one from the environment. |
| Running one test file | From `engine/`: `uv run --group test pytest tests/superchat/test_helpers.py -q`. Run one file at a time; the full suite is large. |
