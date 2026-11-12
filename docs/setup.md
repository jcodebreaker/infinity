# Setup

How to run Superchat on one Docker host, as of 2026-10-19. Everything below lives in
[`deployment/`](../deployment); the commands are the ones its scripts run.

## What runs

One Compose project, `superchat`, from `deployment/compose.yaml`:

| Service | Image | Role |
|---|---|---|
| `postgres` | `postgres:16-alpine` | one database (`omnigent`) with an `engine` schema, created on first init by `deployment/postgres/schemas.sql`; no host port |
| `engine` | `superchat/engine:local` (`deployment/engine.Dockerfile`) | the API, the built web and the published apps in one process; starts each person's host over the Docker socket |
| `host` | `superchat/host:local` (`deployment/host.Dockerfile`) | build only (the `build` profile), so `up` never starts it; the engine starts one container per person from this image |

The host (one container per person, started by the `docker` host provider) is Ubuntu 24.04 with the distro's Python and data stack,
the `claude` and `pi` CLIs, the pptx runtime, Chromium (copied out of Playwright's image) and the
helpers from [`host/`](../host). It has no desktop. Its runner dials the engine back at
`ENGINE_PUBLIC_URL`, which is why the engine port is published. There is no app server: the browser
talks only to the engine ([ADR 0015](adr/0015-no-app-server-the-web-talks-to-the-engine.md)).

The engine publishes its one container port (8000) twice: the web on `127.0.0.1:5173`
(`SUPERCHAT_WEB_PORT`) and the API on `127.0.0.1:8012` (`SUPERCHAT_ENGINE_BIND`,
`SUPERCHAT_ENGINE_PORT`).

## First run

Prerequisites: Docker with the Compose plugin (Docker Desktop, colima, or Docker Engine on Linux),
`openssl`, and roughly 4 CPUs, 8 GB RAM and 40 GB of Docker disk for the first build. Node and pnpm
are not needed; everything builds in Docker.

```sh
./deployment/setup.sh
```

`setup.sh` creates `.env` from `.env.docker.example` (or keeps yours and appends keys the template
gained), fills blank secrets with `openssl rand`, copies `ANTHROPIC_API_KEY`,
`OPENROUTER_API_KEY`, `LITELLM_BASE_URL` and `LITELLM_API_KEY` from your shell when `.env` lacks
them, then runs, from the repo root:

```sh
docker compose --env-file .env -f deployment/compose.yaml --profile build build host
docker compose --env-file .env -f deployment/compose.yaml build engine
docker compose --env-file .env -f deployment/compose.yaml up -d --wait --wait-timeout 300
```

Open http://127.0.0.1:5173. Check the engine with `curl -fsS http://127.0.0.1:8012/health`.

Locally there is no sign-in: the engine runs with `OMNIGENT_LOCAL_SINGLE_USER=1`, so a request
without an identity header is the one local person. The first conversation starts that person's
host.

## The env file

`.env` at the repo root, read by `deployment/compose.yaml`. `.env.docker.example` lists the keys.

| Key | Default | What |
|---|---|---|
| `POSTGRES_PASSWORD` | filled by `setup.sh` | Postgres role password (`POSTGRES_USER` and `POSTGRES_DB` default to `omnigent`) |
| `OMNIGENT_PROXY_SECRET` | filled by `setup.sh` | becomes the engine's `OMNIGENT_AUTH_HEADER_SECRET`; see [Several people](#several-people) |
| `OMNIGENT_VAULT_KEY` | filled by `setup.sh` | 32 random bytes, base64url; seals saved logins and saved model keys |
| `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY` | blank | operator model keys (default config template) |
| `SUPERCHAT_DEFAULT_AGENT` | `superchat-claude` | the harness a new conversation runs on: `superchat-claude` or `superchat-pi`; each person can switch in **Settings > Models** |
| `PROMPT_CACHE_1H` | `1` | becomes `OMNIGENT_PROMPT_CACHE_1H`: both harnesses keep the prompt cache 1 hour instead of 5 minutes; `0` turns it off ([context.md](context.md#prompt-cache)) |
| `FOLD_TURN_LINES_TOKENS` | blank (2000) | becomes `OMNIGENT_FOLD_TURN_LINES_TOKENS` (engine): settled-turn lines, in tokens, past which the engine folds the past into the checkpoint; lower it to see a fold on a short chat ([context.md](context.md#rollover)) |
| `LITELLM_BASE_URL`, `LITELLM_API_KEY` | blank | set both to route every model call through a LiteLLM proxy; root URL without `/v1`, reachable from the engine container (`host.docker.internal` for a proxy on your machine) |
| `SUPERCHAT_WEB_PORT`, `SUPERCHAT_ENGINE_PORT` | `5173`, `8012` | host ports |
| `ENGINE_PUBLIC_URL` | `http://host.docker.internal:<engine port>` | where each host reaches the engine |
| `SUPERCHAT_ENGINE_BIND` | `127.0.0.1` | the address the engine port binds to |
| `DOCKER_SOCKET_PATH` | `/var/run/docker.sock` | the socket the engine starts hosts with, as seen inside the Docker VM; set it only for setups such as rootless Docker |
| `HOST_MEMORY`, `HOST_CPUS`, `HOST_PIDS_LIMIT` | `2g`, `2`, `2048` | per-host limits |
| `OMNIGENT_ARTIFACT_URI` | blank | artifacts in S3/R2 instead of the `/data` volume |
| `OMNIGENT_LOCAL_SINGLE_USER` | `1` | set `0` behind a sign-in broker |
| `IMAGE_REGISTRY`, `IMAGE_TAG` | `superchat`, `local` | which images Compose builds and runs |

The engine lists what the connection serves, and each person's Foundation, Builder and Light picks
are chosen by price until they pick in **Settings > Models**. The Anthropic template sets
`default_model` to name Foundation instead; the key must serve that model.
The config templates do set the `anthropic` family's limits (`context_window: 1000000`,
`max_output_tokens: 32000`), which pi is told; without them pi assumes 128k and 16k. How the
engine picks a config template from these keys is in
[architecture.md](architecture.md#models-and-keys).

One engine serves a deployment, and it treats its own host connections as the only proof that a
host is up. Several engine replicas on one database need `OMNIGENT_MULTI_REPLICA=1` in the engine's
environment, which turns the cross-replica checks back on
([ADR 0004](adr/0004-engine-owns-the-host.md)).

On native Linux, `host.docker.internal` may not reach the engine from a host: set
`ENGINE_PUBLIC_URL` to the Docker bridge address (for example `http://172.17.0.1:8012`) and
`SUPERCHAT_ENGINE_BIND` to the same address.

### Several people

For more than one person, put your organisation's sign-in broker (an authenticating reverse proxy)
in front of the engine and:

1. Set `OMNIGENT_LOCAL_SINGLE_USER=0`, so a request with no identity is refused.
2. Have the broker send the signed-in person's email in `X-Forwarded-Email` (or the header named by
   `OMNIGENT_AUTH_HEADER`) and `X-Omnigent-Proxy-Secret` equal to `OMNIGENT_PROXY_SECRET`. The
   engine honours the identity header only with that secret. The broker must strip identity
   headers a browser sends.
3. Optionally send the organisation in `X-Omnigent-Tenant` (`OMNIGENT_AUTH_TENANT_HEADER`).
4. List admin emails, one per line, in `/data/admins` inside the engine container (the
   `artifact-data` volume). The file is re-read when it changes and promotes a listed person at
   sign-in.

## Builds

`deployment/build.sh` builds both images without Compose:

```sh
deployment/build.sh                 # superchat/engine:local and superchat/host:local
deployment/build.sh engine          # one image
IMAGE_REGISTRY=registry.example/superchat IMAGE_TAG=v1.0.0 \
  PLATFORM=linux/amd64,linux/arm64 PUSH=1 deployment/build.sh
```

It passes each of these that is set: `NODE_IMAGE`, `PYTHON_IMAGE`, `UBUNTU_IMAGE`,
`CHROMIUM_IMAGE` (base images, for registry mirrors), `PYPI_INDEX_URL`, `NPM_CONFIG_REGISTRY`,
`TORCH_INDEX_URL` (package mirrors), `CLAUDE_CODE_VERSION`, `PI_VERSION` (the two CLIs in the host
image, pinned by default to 2.1.302 and 0.87.1, the versions the bundles are verified with), and `CA_BUNDLE=/path/to/ca.pem`, a company CA passed as the `ca_bundle` build secret and
added to each image's trust store. The mirror keys also work in `.env` for `setup.sh`. With
Compose, keep the CA in a Compose file of your own, outside the repo, that declares the
`ca_bundle` secret and adds it to both services' `build.secrets`; pass it last with a second `-f`.

| Image | Stages |
|---|---|
| engine | `web` (pnpm install and `vite build` of `web/app`), `python` (installs `engine/` into `/opt/venv` with the `memory,postgres,s3` extras, CPU-only torch first), `engine` (`python:3.12-slim` with the venv, the web build and `/opt/bundle`: agent bundles, config templates, `start.sh`) |
| host | `chromium` (Chromium alone from Playwright's pinned image), `node` (Node 22 for the CLIs), `venv` (the `omnigent` package, whose host half runs here), `host` (`ubuntu:24.04`, user `superchat`, uid 1000) |

In the engine image, `deployment/engine/start.sh` renders the engine config from the environment
and runs `deployment/entrypoint.py` (config, migrations, app). PID 1 in a host is
`superchat-host`, which only keeps the container alive; the runner and every tool arrive
through `docker exec` from the engine.

For web development, `pnpm install && pnpm dev` runs the web dev server against the engine at
`OMNIGENT_URL` (from `.env.example`, default `http://127.0.0.1:8012`). Engine tests run one file at
a time from `engine/` (see [`engine/README.md`](../engine/README.md)).

## Logs

```sh
docker compose --env-file .env -f deployment/compose.yaml ps
docker compose --env-file .env -f deployment/compose.yaml logs -f engine
docker ps --filter label=omnigent.host        # the hosts the engine started
docker logs -f superchat-host-<key>           # one host
```

A blank required secret fails closed at Compose validation; re-run `./deployment/setup.sh` to fill
it. If a host never starts, the engine cannot reach the Docker socket (check `DOCKER_SOCKET_PATH`)
or the `superchat/host` image is missing. If a host starts but the chat waits, its runner
cannot reach `ENGINE_PUBLIC_URL`.

## Reset

```sh
./deployment/stop.sh        # stop; containers and volumes stay
./deployment/reset.sh       # stop and delete the Postgres and engine volumes and ./data (asks first; -y skips)
```

The hosts are not part of the Compose project: each is a container `superchat-host-<key>` with
its home on a volume `superchat-home-<key>`, and `reset.sh` leaves both. To remove them too:

```sh
docker rm -f $(docker ps -aq --filter label=omnigent.host)
docker volume ls -q --filter name=superchat-home- | xargs docker volume rm
```

## A second stack

A second stack runs beside the first from another checkout, for example to try a branch whose
database schema the first stack's data does not have. From that checkout's root, with its own
`.env` (copy the first one's; `setup.sh` is not needed):

```sh
export COMPOSE_PROJECT_NAME=superchat-train IMAGE_TAG=train
export SUPERCHAT_ENGINE_PORT=8013 SUPERCHAT_WEB_PORT=5174
deployment/build.sh
docker compose --env-file .env -f deployment/compose.yaml up -d --wait --wait-timeout 300
deployment/stop.sh
```

Run every command in a shell with those four values exported: they override `.env`, and
`stop.sh` and `reset.sh` read them too. If `.env` sets `ENGINE_PUBLIC_URL`, export it as well with
the new engine port, or the new hosts dial the first engine. Open http://127.0.0.1:5174.

Isolated: the Postgres and artifact volumes and the network (Compose names them after the
project), the ports, the images (`superchat/engine:train`, `superchat/host:train`), and the hosts,
named `superchat-train-host-<key>` with homes on `superchat-train-home-<key>` (the engine reads the
prefix from `OMNIGENT_HOST_NAME_PREFIX`, which Compose sets to the project name). Shared: the
Docker daemon and its build cache, and whatever model keys the copied `.env` holds. The
`label=omnigent.host` filter under [Reset](#reset) matches both stacks' hosts.

## Secrets

Never commit `.env` or paste its values anywhere. Use an independent value for each key.

| Secret | Where it goes | Leaves the engine? |
|---|---|---|
| `POSTGRES_PASSWORD` | Postgres and the engine's `DATABASE_URL` | no |
| `OMNIGENT_PROXY_SECRET` | the engine (`OMNIGENT_AUTH_HEADER_SECRET`) and your sign-in broker | only to the broker |
| `OMNIGENT_VAULT_KEY` | the engine; seals saved logins and saved model keys with AES-256-GCM and keys published-page view tokens | no |
| `LITELLM_API_KEY` | the engine's model proxy | no: harnesses call the engine with their host's launch token and the engine adds the key |
| saved model keys (bring your own key) | sealed in Postgres | no, same proxy path |
| saved logins (the vault) | sealed in Postgres | only as typed text: `vault_fill` types a value into a field of the host's browser on the saved site; the model never sees it |
| `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY` | the engine and, with the default template, each runner's environment | yes: the default template lists them in `runner_env`, so they are readable inside the host. Use LiteLLM or the BYOK template to keep keys in the engine |

The engine fails closed without `OMNIGENT_VAULT_KEY`: nothing is saved and nothing is revealed.
Rotating it makes every saved login and model key unreadable. An existing Postgres volume keeps
the password from its first init; change it with `ALTER ROLE`, not in `.env` alone. When moving
machines, copy `.env` and the volumes together.

The Docker socket is mounted into the engine only, never into a host.

## Updating

```sh
git pull && ./deployment/setup.sh
```

`setup.sh` keeps `.env` and its secrets and rebuilds both images. A host whose image changed is
recreated at its next launch; its home volume stays.

CI (`.github/workflows/publish-server-image.yml`) publishes both images to GHCR: `edge` from
`main`, semver tags and `latest` from release tags. To run published images instead of building,
set `IMAGE_REGISTRY` and `IMAGE_TAG` in `.env`, then:

```sh
docker compose --env-file .env -f deployment/compose.yaml pull engine
docker compose --env-file .env -f deployment/compose.yaml --profile build pull host
docker compose --env-file .env -f deployment/compose.yaml up -d --wait
```

Any Docker host runs this stack. The engine must start containers, so platforms without a Docker
socket cannot run the hosts.
