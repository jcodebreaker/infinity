# The engine keeps only what Superchat runs

> **Status:** Done 2026-10-17: every cut merged; what went and why is in [`docs/engine-trim.md`](../engine-trim.md). Open follow-ups are listed there.

Omnigent was built to run many coding agents on many platforms: Claude Code, Codex, Gemini, OpenCode, Cursor and others as terminal harnesses; Databricks, Kubernetes, E2B and more as sandboxes; Postgres and CockroachDB; Linux, macOS and Windows jails; a terminal REPL and a thirty-command CLI. Superchat runs exactly two harnesses (`claude-sdk` and `pi`), one host provider (Docker), one database (Postgres), one OS (Linux), and one entry point per process (`entrypoint.py` for the engine, `omnigent host` in the host). Everything else was weight: 60 percent of the package, most of the test suite, and every stale docstring a new developer tripped over.

[ADR 0003](0003-superchat-is-standalone.md) made the fork ours. This record says what that means for the engine: we own it, so we cut it.

## Decision

The engine keeps what the two production processes import, plus the shared layers. Everything else is deleted, not flagged, not stubbed.

| Removed | Why |
|---|---|
| Omnigent's web app, deploy recipes, docs, designs, editors, Slack integration, dev tools, example agents, release scripts (2026-10-16, commit `5d014befe`) | Superchat has its own web and one deployment folder |
| Harnesses other than `claude-sdk`, `claude-native`, `pi`; host providers other than the Docker one (now `docker`); adapters other than Anthropic and OpenAI; tools other than Tavily (2026-10-16, `6380c515e`) | first cut |
| Tavily `web_search` (2026-10-16, `aba39ea7c`) | no bundle enabled it and no deployment passed its key |
| Codex: `harnesses/codex_native`, `inner/codex_*`, `runner/codex`, approval modes, elicitation routes, plugin skills | not a Superchat harness |
| `claude-native`: `harnesses/claude_native`, `native/`, `terminals/`, `runner/native`, the tmux bridge and forwarders | wrapped the Claude Code terminal app; no bundle used it; the SDK harness is the one shape |
| `pi_native` except its credentials module | the pi TUI wrapper; `pi` runs through the SDK executor |
| Databricks: gateway, credential helper, executor, app, identity, model discovery, volumes store, login command | not a deployment target |
| CockroachDB dialect branches and `db/cockroachdb.py` | Postgres only |
| macOS seatbelt and Windows job-object sandboxes | hosts are Linux containers |
| The terminal REPL, `chat.py`, `session_import`, self-update, crash screens, install ledger, diagnostics, smart-routing CLI, demo client tools, the Slack daemon; `cli.py` cut to `host` and the two routing helpers the entry point imports | local-CLI product that Superchat never ships |
| Provider name strings for Gemini, OpenAI agents, ACP, OpenCode, Cursor, Goose, Kiro, Hermes, Qwen, Antigravity | dead branches |
| Web: chart cards, `*.chart.json` and `*.dashboard.json` renderers, canvas blocks, Observable Plot, unreachable components, turbo | no producer; the one live chart path is `*.dashboard.html`; one app does not need turbo |
| Web: side chats and forks | [ADR 0021](0021-one-conversation-messages-link-to-artifacts.md) |

`docs/engine-trim.md` is the running log: each removal with its date, commit and line count.

## Consequences

- Tests that exist only for removed code are deleted with it. The remaining suite becomes the CI Python job, which did not exist before.
- Adding a harness, a host provider or a database back is a feature with its own record, not a flag flip.
- Seams are edited, never shimmed: when a live module imported a removed one lazily, the import and the branch go, and the touched file's tests run before the commit.
- The database is reset once at the start of the move ([ADR 0019](0019-repository-layout-and-layers.md)); legacy formats kept only for old transcripts (chart cards, dashboard JSON, `superchat.*` labels) go with it.

## Considered options

- **Keep removed features behind flags.** Rejected: a flag is code that has to compile, import and be tested. That is the weight we are removing.
- **Keep `claude-native` as a second harness.** Rejected: 37,000 lines for a terminal wrapper no bundle used, and the one place `runner/app.py` branched per harness.
