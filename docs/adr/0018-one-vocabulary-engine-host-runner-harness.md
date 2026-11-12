# One vocabulary: engine, host, runner, harness

> **Status:** Done 2026-10-17: the Computer → host rename (`9d4fd977d`..`a2cbfb745`) applied it to code, config, routes, labels, env, the database, docs and copy.

The per-person container had four names. The UI, the environment variables and the Docker labels called it a Computer; the launcher config and the database columns called it a sandbox; the hosts table called it a managed host; the readiness endpoint reported "runner connected". The word sandbox alone meant three things (the OS process jail, the environment launcher, an iframe), and native meant three (a terminal harness, PowerPoint shapes, the package that held the OS jail). A developer reading the code could not tell which thing a name pointed at.

We fix the words before we move the files. One word per thing, used in code, config, docs and UI copy.

## Decision

| Word | Means | Replaces |
|---|---|---|
| engine | The server. One process. Owns Postgres, the API, sessions, scheduling and the built web. | server, omnigent as a process name |
| host | A person's execution environment. Today a Docker container the engine starts; later a local machine or a hosted VM. Runs `omnigent host`, which already exists. | Computer, sandbox (as a provider), managed host, container |
| host provider | How a host is launched. `docker` today; `local` and `kubernetes` later. | sandbox provider `computer` |
| runner | The per-session process the host launches. Runs one harness. | unchanged |
| harness | The model loop inside a runner: `claude-sdk` or `pi`. | executor, inner, native |
| sandbox | Only the OS process jail (bubblewrap). | — |
| helper | A sub-agent session the assistant starts. Types `worker`, `subworker`, `notes` keep their names. | sub-agent |
| workspace | The session's working directory. | the host's home folder, the tenant id, a Databricks workspace |

`inner`, `managed` and `native` stop being the name of anything. The web's "Computer" tab becomes "Files". Where a person needs a word for their host in copy, it is "your machine".

## Consequences

- Environment variables, Docker labels, container and volume names, routes, config keys and database columns are renamed in one wave ([ADR 0019](0019-repository-layout-and-layers.md), wave 3). The database is fresh at the time of the rename, so no migration carries the old names and the legacy `superchat.*` labels are dropped rather than read.
- `onboarding/sandboxes/computer.py` becomes `hosts/docker.py` (with the launcher contract, types and registry beside it in `omnigent/hosts/`); `server/managed_hosts.py` becomes `server/hosts.py`; the `sandbox:` config block becomes `hosts:` with `provider: docker` (plural: the top-level `host` key is the bind address). The provider lives in `omnigent/hosts/`, not `host/providers/`, because `omnigent/host/` is the HOST layer that runs inside the container (ADR 0019) while the launcher runs in the engine.
- The glossary in `docs/glossary.md` is the reference; a pull request that introduces a second word for one of these things is sent back.

## Considered options

- **Keep "Computer" as the product word and "host" in code.** Rejected: the one-thing-four-names problem was the cost of exactly that split.
- **Rename "runner" too, to "worker".** Rejected: worker is already a helper type, and runner is Omnigent's own word for the thing it is.
