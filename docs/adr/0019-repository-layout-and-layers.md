# Repository layout and layers

> **Status:** Done 2026-10-17: `web/`, `host/`, `engine/` (flattened, `inner` → `harnesses`, `engine/agents/`), `deployment/`, `docs/` in place; both import-linter contracts pass and run in CI; the engine README carries the layer map and where-is table.

After the trim the engine package was still one connected blob. Every module could reach every other through a lazy import, product code lived outside the product folder (416 lines outside `omnigent/superchat` mentioned it; `runner/app.py` alone had 140), the system prompt lived under `deployment/`, and documentation sat in six places. Nothing told a developer where the engine ends, where the host begins, or where to look for the orchestrator.

We keep one Python package and one deployment, and make the layers visible and enforced. The monolith stays a monolith; it reads like separate projects.

## Decision

```
superchat/
├── web/            the browser UI: app/ (was apps/web) and packages/
├── engine/         the Python monolith, package `omnigent`, import paths unchanged
│   ├── omnigent/
│   │   ├── server/                       ENGINE   API, sessions, scheduler, auth
│   │   ├── superchat/                    PRODUCT  features, tools, prompts, memory, artifacts, decks, helpers
│   │   ├── db/  stores/                  DATA     Postgres models, migrations, stores
│   │   ├── host/  runner/  harnesses/    HOST     runs inside a person's host
│   │   └── spec/ runtime/ llms/ tools/ policies/ entities/ context/ models/ util/   SHARED
│   ├── agents/     the system prompt and helper types: templates, the render script, the rendered bundles
│   ├── tests/
│   └── README.md   the layer map and the where-is table
├── host/           what ships into a person's host besides the package: bin/, share/, tests/
├── deployment/     build and run only: Dockerfiles, compose, setup scripts, engine config templates
└── docs/           one folder, one file per subject, adr/
```

- `engine/omnigent/omnigent` flattens to `engine/omnigent`. The import path `omnigent.*` does not change.
- `inner/` merges into `harnesses/`; `native/`, `terminals/`, `repl/`, `session_import/` and the legacy CLI modules are deleted ([ADR 0020](0020-the-engine-keeps-only-what-superchat-runs.md)).
- `runner/knowledge` and `runner/live` are Superchat features and move under `superchat/`, keeping their runner-side entry points.
- An import-linter contract in `pyproject.toml` enforces the layers: HOST never imports `server`; nothing below `superchat` imports it; CI fails on a violation.
- The engine's `README.md` holds the layer map and a where-is table (system prompt, orchestrator, helpers, tools, memory and recall, artifacts, Postgres, host provider). It is updated in the same pull request as a move.
- Docs consolidate into `docs/`: `setup.md`, `architecture.md`, `glossary.md`, `conversation.md`, `context.md`, `artifacts.md`, `decks.md`, `engine-trim.md`, `adr/`. Historical plans are deleted; git keeps them.

## Consequences

- Dockerfiles, CI, `pyproject.toml`, `vitest.config.ts`, `biome.json` and every doc path change in the move wave. The move is one wave of pure moves with no logic changes, verified by rebuilding both images and running the smoke test.
- The rendered agent bundles stop being committed twice: `engine/agents/render-agents.mjs` runs at image build and in the test fixture.
- `host/` holds product code that runs in a person's host (`superchat-dashboard`, `superchat-deck-export`, `superchat-page-browser`, `superchat-browser`, `start.sh`, the dashboard template, the deck-export vendor bundle, the pptx runtime and skill) and its tests. `deployment/` keeps only what builds and runs images.

## Considered options

- **Split into separate packages (engine, runner, product).** Rejected: the host installs the same wheel the engine runs, and the runner imports product handlers at dispatch time. Separate packages would mean publishing three wheels and a shared-contracts package for one deployment.
- **Leave the layout and only write the README.** Rejected: a map of a folder with sixty siblings is still a folder with sixty siblings.
