# Engine trim tracker

`engine/omnigent` is a full copy of Omnigent: every harness, provider,
sandbox and deploy target it supports. Superchat uses a small part of it
(Claude SDK and Pi harnesses, `superside-chat`). This file tracks what we carry but
don't use, so it can be removed before production.

**How to use it**

- A feature starts needing something listed under **Candidates** → move its
  row to **Needed** and add one line saying which feature needs it.
- Before production, remove everything still under **Candidates** (code,
  tests, docs, dependencies), then run the engine and app test suites.
- Removing is done in small commits, one row at a time, so a mistake is easy
  to revert.

## Needed

Paths are relative to `engine/`. Checked against the code 2026-10-19: the flatten moved `inner/` to `harnesses/`, the host rename replaced the Computer pieces, and side chats are gone (ADR 0021).

| Part | Path | Needed by |
|---|---|---|
| Server, runner, host daemon | `omnigent/server`, `omnigent/runner`, `omnigent/host` | Everything |
| superside-chat | `omnigent/superchat`, `omnigent/context` (rollover: `context/rollover.py`; docs in `docs/context.md`) | Conversation, Helpers, Rounds, Activity |
| Claude SDK harness | `omnigent/harnesses/claude_sdk_executor.py`, `claude_sdk_harness.py` | Bundle `superchat-claude` (the default) |
| Pi harness | `omnigent/harnesses/pi_executor.py`, `pi_harness.py`, `pi_settings.py`, `pi_credentials.py` | Bundle `superchat-pi` (`SUPERCHAT_DEFAULT_AGENT` / `OMNIGENT_SUPERCHAT_DEFAULT_AGENT`, or a switch in Settings) |
| Memory | `omnigent/memory`, `omnigent/superchat/memory`, `omnigent/server/memory_upkeep.py`, `omnigent/server/routes/session_memory.py` | Memory Profile, Upkeep |
| LLM client: Anthropic | `omnigent/llms/adapters/anthropic.py` | Rollover summaries, Upkeep |
| LLM client: OpenAI | `omnigent/llms/adapters/openai.py` | Memory search embeddings (`text-embedding-3-small`) |
| Stores, DB, migrations | `omnigent/stores`, `omnigent/db` | Everything |
| Header auth with shared secret | `omnigent/server/auth.py` | Sign-in broker → engine identity |
| Host provider | `omnigent/hosts/` (`docker.py`), `omnigent/server/hosts.py`, `omnigent/server/host_keepalive.py`, `omnigent/server/host_registry.py` | The Assistant's runner in the person's host |
| Host status | `omnigent/superchat/host_status/routes.py` | Ready and launch status (`GET /v1/sessions/{id}/host`) |
| Local browser backend | `omnigent/tools/browser_backend.py` | `browser_*` on the host's own screen |
| OS tools | `omnigent/harnesses/os_env.py`, `omnigent/tools/builtins/os_env.py` (`sys_os_*`) | The Assistant and Helpers acting in the host |
| Browser prompt instruction | `omnigent/runtime/prompt.py` | Steers the Assistant to `browser_*` |
| Agent spec / bundles | `omnigent/spec`, `agents/templates` rendered into `agents/bundles/superchat-{claude,pi}` | Every session |
| Built-in tools in use | `omnigent/tools` (`session_history`, `browser_*`, scheduled tasks) and the Superchat tools under `omnigent/superchat` (`memory_*`, `start_helper`, cards, artifacts, decks) | The Assistant and Helpers |

## Candidates

### Engines (harnesses) other than Claude SDK

| Part | Path | Why we likely don't need it |
|---|---|---|
| Native CLI harnesses (claude, codex, pi-native, cursor, devin, goose, hermes, kimi, kiro, opencode, qwen, antigravity) | `omnigent/harnesses/*_native`, `omnigent/inner/*_native_*`, `omnigent/native`, `omnigent/cli_native.py` | Superchat runs Claude SDK and Pi only |
| Other SDK harnesses (codex, openai-agents, antigravity, copilot, cursor, databricks, goose, hermes, kimi, qwen) | `omnigent/inner/{codex,openai_agents_sdk,antigravity,copilot,cursor,databricks,goose,hermes,kimi,qwen}_*` | Same |
| ACP harnesses | `omnigent/inner/acp_*`, `omnigent/acp_cli_harnesses.py` | Same |
| Native-CLI rollover mode (`omnigent.context.mode=rollover`) | parts of `omnigent/context/rollover.py`, `runner/native/orchestration.py`, `rollover/README.md` | Only `superside-chat` is used |
| Harness install/availability/plugins | `omnigent/harness_*.py`, `omnigent/install_ledger.py` | One fixed engine |

### Model providers and routing

| Part | Path | Why |
|---|---|---|
| Bedrock, Vertex, Gemini, Databricks LLM adapters | `omnigent/llms/adapters/{bedrock,vertex,gemini,databricks}.py` | Anthropic + OpenAI only |
| Databricks AI gateway, tokens | `omnigent/databricks_ai_gateway.py`, `omnigent/inner/databricks_token.py` | Not used |
| Smart routing | `omnigent/smart_routing_cli.py`, routing parts of `omnigent/runner` | One model per bundle |
| Model signing / egress / credential proxy | `omnigent/inner/model_sign*`, `model_egress.py`, `credential_proxy.py` | Check before removing: may be needed for the computer runner |

### Sandboxes and deploy targets

| Part | Path | Why |
|---|---|---|
| Third-party sandbox providers (blaxel, boxlite, cwsandbox, daytona, e2b, gensee, islo, kubernetes, microsandbox, modal, openshell) | `omnigent/onboarding/sandboxes/*.py` (not `computer`, `base`, `registry`, `agent_sandbox*`) | Superchat uses its computer |
| Local OS sandboxes (bwrap, seatbelt, Windows job object, seccomp) | `omnigent/inner/{bwrap,seatbelt,windows_jobobject}_sandbox.py`, `_seccomp.py` | Check: may be used by the computer runner |
| Deploy recipes (blaxel, boxlite, cloudflare, cockroachdb, databricks, daytona, e2b, fly, gensee, hf-spaces, islo, kubernetes, microsandbox, modal, openshell, railway, render, tailscale) | `deploy/*`, `railway.toml`, `render.yaml` | Superchat deploys with its own `infra/` |

### Clients and surfaces Superchat replaces

| Part | Path | Why |
|---|---|---|
| Omnigent web app | `web/` | Superchat has its own app |
| Terminal REPL, pickers, conversation browser | `omnigent/repl`, `omnigent/terminals`, `omnigent/conversation_browser.py`, `omnigent/_terminal_picker_theme.py` | No terminal UI in Superchat |
| CLI onboarding and setup | `omnigent/onboarding` (except sandboxes in use), `omnigent/cli_*.py` | Check: the host daemon still starts from the CLI |
| Editor and chat integrations | `editors/vscode`, `integrations/slack`, `omnigent/integration_daemon.py` | Not used |
| Client SDKs | `sdks/` | Superchat calls the HTTP API directly |
| Session import | `omnigent/session_import` | Not used |

### Tools Superchat doesn't use

| Part | Path | Why |
|---|---|---|
| Desktop-app browser relay | `runner/tool_dispatch.py` `_execute_browser_tool` relay path, `server/routes/sessions/routes_browser.py` | Superchat uses the local backend. Candidate, but kept: generic, and the default when the variable is unset |
| `web_fetch` and its sandboxed web-research sub-agent | `omnigent/tools/builtins/web_fetch.py`, `__web_researcher` dispatch in `omnigent/runner/tool_dispatch.py` | Web search calls Tavily directly |
| Other web search backends (OpenAI native, Perplexity, Nimble, DuckDuckGo, …) | `omnigent/tools/builtins/web_search*.py` except `web_search_tavily.py` | Tavily only |

### Examples, docs and tooling

| Part | Path | Why |
|---|---|---|
| Example agents | `examples/{aws_analyst,debby,deep-research,extensions,kimi_hello.yaml,polly,remy,scribe,sentinel}` | Only `super-chat` is a reference |
| Policies examples (nessie) | `omnigent/inner/nessie` | Not used |
| Design docs, feature map, stray mockup | `designs/`, `feature-map/`, `muse-clone.html` | Upstream material |
| Upstream skills | `engine/omnigent/.claude/skills/*` (harness e2e, polly, load test) | For harnesses we don't run |
| Load test, benchmarks | `dev/loadtest`, `dev/benchmarks` | Keep only if we load-test |

## Removed

| Part | When | Commit |
|---|---|---|
| Superchat's own engine package `omnigent/superchat` (notes memory, feed, episodes, goals, asks, skills, context provider) | 2026-10-03 | `bfe6886e` (replaced by superside-chat) |
| Omnigent's web app, deploy recipes, docs, designs, feature map, editors, Slack integration, dev tools, example agents other than `super-chat`, release scripts, pnpm workspace | 2026-10-16 | engine-only: groups A and B of `engine-axe-list.md` |
| `infra/` and `scripts/` (moved into `deployment/`; old Dockerfiles, compose overlay and Railway config deleted) | 2026-10-16 | same |
| `web_search` and its Tavily backend (no bundle enabled it and no deployment passed the key) | 2026-10-16 | web search returns later as its own capability |
| Databricks: gateway, credential helper, executor, app, identity, model discovery, volumes store, login and configure branches; with them `onboarding/setup.py`, `ucode_setup.py`, `ucode_cleanup.py`, `runtime/credentials`, `inner/claude_gateway_shim.py` (28,711 lines) | 2026-10-16 | `dcc017c00`, ADR 0020 |
| CockroachDB dialect branches, `db/cockroachdb.py`, the CRDB DDL lock in `tests/db/conftest.py` (1,218 lines) | 2026-10-16 | `5a850efda`, ADR 0020 |
| macOS seatbelt and Windows job-object sandboxes, `sandbox/bwrap.py` (unreachable); `_default_sandbox_for_platform()` is always `linux_bwrap` (5,972 lines) | 2026-10-16 | `c7a18240d`, ADR 0020 |
| Dead harness and provider strings (OpenAI agents, ACP, OpenCode, Cursor, Goose, Kiro, Hermes, Qwen, Antigravity, `GEMINI_API_KEY` forwarding; Gemini stays as a model vendor a gateway can serve) and the `uv.toml` exemptions (245 lines) | 2026-10-16 | `13fa2cf9a`, ADR 0020 |
| Flatten (ADR 0019): `engine/omnigent/omnigent` → `engine/omnigent`, `inner` → `harnesses`, `runner/knowledge` and `runner/live` → `superchat/`, bundles and templates → `engine/agents/`; deleted `sdks/ui` (`omnigent_ui_sdk`) with its 31 tests and `examples/super-chat`; `rollover/` docs → `docs/rollover` (17,955 lines) | 2026-10-17 | `8042e8eae`..`4825950c8`, merge `5b24a18a6`, ADR 0019 / 0020 |
| Engine: `superchat/side_chats` (seeds, forks, archiving), the `side_chat_open` tool, `list_chats` prompt lines, the Side Chat sections of the assistant bundle, the `side_chat` activity source, transcript seed and fork fields; the live-server test suites (`tests/e2e`, `tests/integration` journeys, `live_server.py`, 21 e2e-only resources, `omnigent_client.LocalServer`). Kept: helpers, the superside-chat context mode, family stream and search, the recall index columns (84,956 lines) | 2026-10-17 | `25f3b68dc`..`d327751fc`, merge `043845547`, ADR 0021 / 0020 |
| Tests whose subject is gone and dead engine code: polly/debby seeders and `resources/examples`, `resolve_repo_symlink`, `tests/scripts`, `tests/benchmarks`, `test_builtin_bundles`, `test_resolve_agent_*`, `test_recording_carveout_scope`, the Daytona and kiro deploy tests, the deck legacy-kit theme test, the darwin seatbelt case, `tests/helpers/ui_server_compat.py`, `test_ui_server_compat.py`, the `--omnigent-server-url` option, `_platform.py`, the chat-ui React Native markdown path (`linkifyExplicitUrls`) (about 4,700 lines) | 2026-10-17 | `4efee2e31`..`f1b6c7597`, merge `f540c687c`, ADR 0019/0020 |
| Legacy CLI: `cli.py` cut from 11,545 to 1,797 lines (root group, `host` group and its six subcommands, `main`); `repl/`, `chat.py`, `update_check`, `crash_*`, `install_ledger`, `cli_diagnostics`, `resume_dispatch`, `integration_daemon`, `client_tools/`, `session_import`, `_env_compat`, `cli_invocation`, `conversation_browser`, `_terminal_picker_theme`, `cli_config`, `diagnostics.py`, `_runner_startup.py`, `onboarding/sandboxes/bootstrap.py`, `host/local_server.py`, `host/_daemon_entry.py`, `runner/repl_terminal.py` (the REPL terminal auto-launched beside every session), the `omni` alias; Databricks follow-ups: copy-on-write sandbox, UC function tools, MCP `databricks_profile`, `databricks_cli` credential proxy, `kind: databricks` provider, router Databricks auth, Lakebase refresh, `ucode_state`, `databricks-sdk` extra; Codex follow-ups: onboarding detection and install, `codex_auth_readiness`, `configure_models`, `interactive`, `codex_model_vocabulary`, `CODEX_ACCESS_TOKEN` forwarding. Routing helpers now `server/routing_settings.py` (106,926 lines) | 2026-10-17 | `34183c66b`..`4c3719384`, merge `7a3f7c1f0`, ADR 0020 |
| Web: side chats and forks. `features/side-chats`, the Chats popover (side chats, forks, archive), the fork action and thread, the old sidebar, "Chat with this folder", the auto-archive setting, `rpc/side-chats.ts` (now `rpc/chats.ts` with transcript, send, interrupt, project, setFolder), the `fork_summary` block, `ThreadMessage.forks`, `ThreadMessagePage.lineage`, the `fork-*` tokens. Conversation components moved back under `pages/app/conversation` (9,969 lines) | 2026-10-17 | `e81b4e92a`..`3d58abbbd`, ADR 0021 |
| Native harness cluster: `harnesses/codex_native`, `harnesses/claude_native`, `harnesses/pi_native` (credentials kept as `inner/pi_credentials.py`), `omnigent/native`, `runner/native`, `runner/turn_routing.py`, native terminal pieces, `server/routes/imports.py`, native branches in `runner/app.py`, `server/routes/_sessions/helpers.py`, `cli.py`, `host/connect.py`, `host/frames.py`, root `tests/e2e_ui`. `omnigent/terminals` core kept for `sys_terminal` and terminal attach (398,338 lines) | 2026-10-17 | `a1cb978fb`..`7e1a4e7f5`, ADR 0020 |
| Web: `packages/charts`, the `*.chart.json` / `*.dashboard.json` renderers, chart cards and thumbnails, the canvas block and `ui-web/canvas`, Observable Plot, the `chart` and `canvas` message blocks, dead event kinds (`app_connect`, `child_bot`, `choice`, `computer`, `connect`), `bot-secrets`, `cron`, `avatar-motion`, `AuroraBackground`, `ConversationHeader`, transcript grouping, chart and canvas dev previews, turbo and stale ignore entries. Old transcripts show "This chart was made with an older version"; `*.dashboard.html` pages stay (14,020 lines) | 2026-10-16 | `1bc32b5ca`..`88bf660fe`, ADR 0020 |
| Harness switch clones: the per-session `"<name> (switch ag_…)"` agent row a switch made, the clone arguments of `switch_conversation_agent`, the built-in lookup by `bundle_location` and the display-name cut `_SWITCH_SUFFIX_RE`; a switch binds the built-in, and startup rebinds old copies (`rebind_switch_clones`) and deletes them | 2026-10-19 | `0919b6aa8`, ADR 0029 amendments |
| Claude's native task tools (`TaskCreate`, `TaskUpdate`, `TaskList`, `TaskGet`): now disallowed on every Claude session; with them `ExecutorConfig.checklist`, `CLAUDE_CODE_ENABLE_TODO_TOOLS` and the relaunch when the flag changed | 2026-10-19 | `1d639c7b8`, ADR 0006 / 0029 amendments |
| pi turn end: the `willRetry` branch on `agent_end`, the 10 s post-error drain (`_TURN_STDOUT_ERROR_DRAIN_TIMEOUT_S`) and `retry.provider.timeoutMs`; a turn ends on `agent_settled` | 2026-10-19 | `94f3fa745`, ADR 0029 amendments |
| The wake's cross-replica polling loop (0.5 s sleeps re-reading the hosts row); a launch or wake waits on `HostRegistry.wait_for` | 2026-10-19 | `a764bce19`, ADR 0004 amendment |
| `SqlAlchemyRoundStore.open_turn_rounds` (the Sweep's start read of Rounds with an open turn only), replaced by `rounds_since` | 2026-10-19 | `b471333c8`, ADR 0029 amendments |

## Follow-ups from the web cut

- Host folder (2026-10-17, `38eb06fc0`, `df9118488`): `deployment/computer/*` is `host/bin`, `host/share`, `host/tests` (ADR 0019). Image, service and Dockerfile names unchanged until the Computer → host rename wave (closed 2026-10-17, `9d4fd977d`..`a2cbfb745`: `superchat/host`, `deployment/host.Dockerfile`, service `host`). `host/tests/test_dashboard.py` needs pandas, which the engine venv lacks. Closed 2026-10-17, `7493c3b88`: CI's `host-tests` job runs `host/tests` with pandas and pytest.

- Web move (2026-10-17, `edda299ee`, `100d359bd`): `apps/web` is `web/app`, `packages` is `web/packages` (ADR 0019). Package names unchanged. Root `NOTICE` still names paths that no longer exist (`infra/sandboxes`, `features/decks/edit/bridge.js`); rewrite it in the docs wave.

- Web structure pass (2026-10-17, `241f34937`..`bcb7e66f9`): features no longer import from pages; `pages/muse` is `pages/app`; CSS prefix `rk-` is `sc-`. Two seams remain: `lib/useMuseLiveState` composes `features/approvals` and `features/computer`, and `components/conversation` imports features because the fork thread renders the message bubble. Both resolve when side chats and forks go (ADR 0021).
- `web/packages/chat-ui` `markdown.test.ts` failed on a dropped React Native markdown package. Closed 2026-10-17, `2ad649d8f`: `linkifyExplicitUrls` (the native parser setup) and its test are deleted.

- Root `NOTICE` still credits the nao-derived chart code that no longer exists; trim the entry.
- `pnpm check` failed on a pre-existing error in `web/packages/adapters` (`BodyInit` not found in `src/omnigent/artifacts.ts`). Closed 2026-10-17: the body is typed through `RequestInit`, and `pnpm -r check` is clean.
- `tool-presentation.completeness.test.ts` failed because the `superchat-claude` bundle declares no `builtins`. Closed 2026-10-17, `5c02e247a`: the bundle is right; the test reads the `tools: allow:` lists. 23 enabled tools still have no presentation entry and are listed in `NOT_YET_PRESENTED` (a shrink-only list) for the web side to register.

## Built on top (not removals)

- 2026-10-17: ADR 0021 both halves (`a2812bace`..`d3e2933e0` web; `e8c6a2656`..`aebbbf778` engine: `message_artifacts` table, `artifacts.item_id`, hint route, relink route with steer) and ADR 0022 engine half (`5ccbb5d47`..`cf3080ffe`: `report_progress`, `raise_flag`, throttle, fold, activity lines, timing logs). Contract: `docs/conversation.md`. Web work strip merged (`ca6bd26f4`..`ba2adf5aa`): one strip per reply replaces `HelperTracker`; Show details focuses the helper's row in the activity panel; "Answered while the work continues" is timing-derived until the engine exposes first-token time on the item.
- 2026-10-17: layering wave (`4efee2e31`..`f1b6c7597`, merge `f540c687c`): both import-linter contracts pass (runner wire shapes in `runtime/runner_wire.py`, `server/subagent_route_policy.py`, `entities/session_project.py`, `HostPresence` protocol, product registries in `tools/manager.py` and `runtime/policies/hooks.py` filled by `superchat/features.py`); CI `engine` and `host-tests` jobs; the engine image renders bundles in its `agents` stage; `uv.lock` regenerated with ruff 0.17; `inference_catalog` no longer reads the removed `ProviderEntry.display_name` (every BYOK catalog had reported discovery unavailable).
- Docs consolidated per ADR 0019 (`1cd7fcda0`..`a2009d045`): this log moved here from `docs/super-chat/ENGINE-TRIM.md`.
- 2026-10-17: Computer → host rename, ADR 0018 / wave 3 of ADR 0019 (`9d4fd977d`..`a2cbfb745`, not a removal): the provider is `omnigent/hosts/` (`docker.py`, `launcher.py`, `types.py`, `registry.py`) named `docker`; `server/hosts.py` and `server/host_keepalive.py`; labels `omnigent.host.owner` / `omnigent.host.key` (the `superchat.*` fallback dropped); `OMNIGENT_HOST_*` and `OMNIGENT_HOST_PROVIDER`; the `hosts:` config block; route `/v1/sessions/{id}/host` (`superchat/host_status`); knowledge status field `host`; image `superchat/host`; web `features/files`, `engine/host.ts`, `HostStatusPanel`, `hostReady.ts`; copy says "your machine"; the unused `hosts.computer_control_token` column and its migration deleted.

## Follow-ups from the flatten

- Import-linter contracts reported direct-import violations (HOST → server and shared → superchat). Closed 2026-10-17, `4efee2e31`, `a12b346f3`, `fc7402988`: both contracts pass. Runner wire shapes moved to `runtime/runner_wire.py`; the subagent routing policy to `server/subagent_route_policy.py` and the family table to `models/model_fallbacks.py`; `runner/routing.py` types the host registry as a `HostPresence` protocol; `context/rollover` reads `entities/session_project.py`; `tools/manager` and `runtime/policies` take product tools, policies and owner budgets from registries `superchat/features.py` fills at import.
- Bundles are still committed; render at image build and in the test fixture. Image half closed 2026-10-17, `3fb20de60`: the engine image renders them in its `agents` stage. The committed copy stays for the tests (the drift test compares it with a fresh render); delete it once the fixtures render.
- `uv.lock` still listed `omnigent-ui-sdk`. Closed 2026-10-17, `7493c3b88`.
- `decks/test_patches.py::test_decks_built_by_every_released_kit_can_switch_theme` failed (2 cases). Closed 2026-10-17, `7f913d509`: deleted. The product rename changed the deck markers (`data-nova-*` → `data-superchat-*`) and the legacy table text, so decks from the Nova-era kits cannot be adopted, and ADR 0020's database reset means none exist.

## Follow-ups from the engine side-chat cut

- Web adapter types still name `lineage.kind "side"`, `seed_item_id` and `includeSeed` (`web/packages/adapters/src/omnigent/client/{transcript,sessions}.ts`); drop with the chip work.
- Dead live-UI test infra: `tests/helpers/ui_server_compat.py`, `tests/test_ui_server_compat.py`, the `--omnigent-server-url` option. Closed 2026-10-17, `782ddf35c` (with the guardrail's base-URL check that read the option).
- `CONTEXT.md` still defines Side chat and Fork; `openapi.json` is hand-edited and stale (regenerate in the docs wave with `engine/scripts/dump_openapi.py`).
- `tests/server/test_app.py` (7 fail, 3 errors), `test_sessions_crud` (1), `test_manager` web_search (3), `test_me_muse` (1), `test_superside_chat_subagent_wake` (3) were already failing before these cuts. Closed 2026-10-17, `b32238e17` (fixed) and `7f913d509` (deleted with their subject: polly/debby seeders, codex label guard, web_search).

## Follow-ups from the legacy CLI cut

- `tests/e2e`, `tests/_helpers/live_server.py` and the 49 test files that spawn `omnigent server` run a command that no longer exists. Delete them with the engine side-chat cut; the Superchat smoke test against the compose stack replaces them (ADR 0019 CI job).
- `cli_auth.py` stays as the runner and host server-client factory; its Databricks org-id and slice-key headers and the `databricks_request_headers` name are dead weight to rename and trim in the flatten wave.
- Smart routing tables (`model_fallbacks`, `pi_model_compatibility`, `model_override`, `AUTO_NATIVE_ROUTING_HARNESSES`, `_COUNTERPART_HARNESS`) still carry Codex and native names; cutting them changes routing candidates and needs its own record.
- `sdks/ui` (`omnigent_ui_sdk`) is unused by the engine; the Dockerfiles still install it. Remove with the flatten wave. `sdks/python-client` is still imported by `tools/local.py`.
- `server/schemas.py` keeps native wire fields the web contract names; cut them together with the web contract.

## Follow-ups from the web side-chat cut

- Engine half of ADR 0021 still to cut: `superchat/side_chats`, the `superside-chat` seed plumbing in `context/labels.py` and `context/rollover.py`, the `side_chat_open` tool, the side-chat archiving job. Web keeps the `side_chat` activity source and the `side_chat_open` tool title until then.
- `SuperchatGlyphs` exports four glyphs nothing uses now (`ConversationGlyph`, `WaitingGlyph`, `SettingsGlyph`, `SidebarGlyph`).

## Follow-ups from the native cut

- Codex SDK-family vocabulary remains in onboarding (`ambient`, `provider_config`, `harness_install`, `configure_models`, `detected`, `codex_auth_readiness`, `ucode_state`), models (`codex_model_vocabulary`, `model_catalog`, `model_override`, `model_fallbacks`), `runtime/workflow.py`, `cli_config.py`, `server/smart_routing.py`, `server/schemas.py`, `runner/subagent_routing.py`, `host/frames.py`; `inner/hook_scripts/subagent_router.py` still names `codex-native`; `CODEX_ACCESS_TOKEN` is still forwarded in `host/connect.py`. Cut with the legacy CLI.
- `chat.py` imports the deleted `databricks_executor`; it goes with the legacy CLI.
- Tests needing `asgiref` or `opentelemetry.sdk` failed to collect in the engine venv. Closed 2026-10-17, `7493c3b88`: both are in the `test` group, and the suite collects with no errors.

## Follow-ups from the Databricks cut

- Still reference Databricks and are on the legacy-CLI cut list: `cli_auth.py`, the fronted-server detection in `cli.py`, `server/smart_routing.py` (`databricks_profile`, ambient SDK auth), `inner/credential_proxy.py`, `runner/uc_function.py`, `tools/mcp.py` `databricks_profile`, Lakebase token refresh in `db/utils.py`, the routing tables in `models/model_fallbacks.py`, `pi_model_compatibility.py`, `model_override.py`, `ucode_state.py`, the `kind: databricks` schema in `spec/types.py`, `spec/parser.py` and `provider_config.py`.
- `sandbox/copy_on_write.py` only ever accepted the removed OpenAI-agents harness; its gate now rejects every harness. Delete the feature whole with its `inner/os_env.py` import.
- The executors' `gateway=True` path is NOT Databricks-only: `runtime/workflow.py` sets it for every `kind: gateway` provider, which the deployment config templates use for LiteLLM. Kept. (Corrected 2026-10-17.)
- `databricks-sdk` stays in the `all` extra until the references above are gone; `uv.lock` was already stale before the cut.

## Follow-ups from the layering and CI wave

- `runner/routing.py` (`RunnerRouter`) is server-side code under `runner/`: only the server, Superchat's approvals and type hints import it, and it routes over the server's tunnel registry (`runner/transports/ws_tunnel/registry.py`, check that one's importers too). Move it to `server/` in a layering wave; the `HostPresence` protocol then goes.
- The deck kit's legacy adoption (`_adopt_markers`, `_legacy_kits`, `kit/legacy.json`, the `_LEGACY_*` patterns, `test_every_released_block_text_is_in_the_shipped_table`) only serves decks saved by pre-marker kits, which were all Nova-era; cut it whole.
- Runner tests that still drive `external_session_status`, which the runner no longer takes: the codex-native cases of `test_native_subagent_inbox_delivery.py` (32 fail), `test_runner_idle_active_work.py` (6), `test_suppress_recovery_turn.py` (4), `test_native_subagent_status_laundering.py` (2). Rewrite on `mark_subagent_terminal_and_wake` or delete with their native subject.
- Deleted 2026-10-17 with the native harness cluster (`a1cb978fb`..`7e1a4e7f5`, ADR 0020): `test_native_subagent_inbox_delivery.py` forwarder cases (8 tests, plus the `codex-native` and `active` parameters of `test_recovered_child_continues_same_dispatch_before_delivering_result`), `test_native_subagent_status_laundering.py` (2), `test_runner_idle_active_work.py` native status cases (5), `test_suppress_recovery_turn.py::test_native_activity_during_initialization_distinguishes_turns_from_repaints`.
- Deleted 2026-10-17 with the native harness cluster (same record): `test_async_granted_surface.py` native relay cases (2), `test_subagent_model_inheritance.py::test_child_shares_parent_vendor_across_sdk_native_split_inherits`, `test_stream_failure_diagnostics.py::test_stream_drop_after_required_terminal_exit_reports_the_exit`.
- Deleted 2026-10-17: `tests/runner/test_orchestrator_skill_injection.py`; the `build-omnigent` skill went with `onboarding/agent` in the first cut (`6380c515e`, ADR 0020).
- Deleted 2026-10-17: the four `_clamp_entries_to_output_caps` tests in `tests/harnesses/test_pi_credentials_output_cap.py`; the Pi gateway output-cap clamp went with Databricks (`dcc017c00`, ADR 0020).
- Deleted 2026-10-17: `tests/server/integration/test_session_todos_process_restart.py`; `external_session_todos` (the native Plan) went with the native harness cluster (`7e1a4e7f5`, ADR 0020).
- Deleted 2026-10-17: `test_session_host_launch.py::test_inline_launch_stamps_terminal_view_label_at_creation` and the `OMNIGENT_WRAPPER_COMMAND` case of `test_message_relaunch_deterministic_failure_persists_error_turn`; the REPL terminal label and the wrapper setup hint went with the legacy CLI (`34183c66b`..`4c3719384`, ADR 0020).
- Deleted 2026-10-19: `tests/test_setup_web_ui_build.py`, `tests/github/test_coverage_total.py`, `tests/github/test_release_notes_workflow.py`, `tests/dev/test_verify_omnigent_feature_map.py`, `tests/extensions/test_hello_extension.py` and `tests/test_ui_fixture_wiring.py`; the web UI build setup, the coverage and release-notes workflows, the feature-map check, the hello extension and the UI fixture wiring they tested had already gone with earlier cuts (ADR 0020), and the thirty-one tests failed on every CI run since.
- The full engine suite has not been run on a laptop since the cuts; the first CI run of the `engine` job will show what else is red. The job is not in `deploy-production`'s `needs` yet; add it once it is green.
- `docs/setup.md` has no GitHub App section, though three engine modules now point at it for one.
- `server/subagent_route_policy.py` still offers `claude-native` / `codex-native` counterparts (`_COUNTERPART_HARNESS`), with the smart routing tables noted under the legacy CLI cut.
- Deleted 2026-10-20 with ADR 0031 step 3 (the chain arm passed on pi and Claude): the restart and idle-return summaries and the catch-up wait in `runner/app.py` (`_start_superside_chat_catch_up`, `_superside_chat_catch_up`, `_await_superside_chat_catch_up`, `_maybe_superside_chat_cold_rollover`, `_superside_chat_catching_up`, `_superside_chat_roll_over_now`, the cold-overflow and catch-up sets, the idle return's blocking summary), the Super Chat replay of the checkpoint pair and kept tail, `CATCHING_UP_NOTE`, and the `OMNIGENT_CONTEXT_ASSEMBLE` flag; `test_idle_rollover_skips_when_session_model_unresolvable` went with them.
- Deleted 2026-10-20 by owner decision (ADR 0031 decision 4, a warm session is replaced only when it needs compacting): the 12-hour return, cache-expiry and Claude model-switch handoffs in `superchat/rollover.py` (`expiry_handoff`, `resolve_cache_lifetime_seconds`, `OMNIGENT_EXPIRY_HANDOFF`, `OMNIGENT_PROMPT_CACHE_LIFETIME_SECONDS`) and `runner/app.py` (the request-start clock `_last_request_start`, `_last_turn_model`), their deploy wiring (`EXPIRY_HANDOFF`, `PROMPT_CACHE_LIFETIME_SECONDS` in compose, the engine templates and `docs/setup.md`) and the probe case `offsite_expiry.json`. The gap line stays, from the newest stored item.
