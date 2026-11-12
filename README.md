# Infinity

Infinity gives each person one persistent assistant that keeps their conversation, memory and
files, works in the background on its own machine, and comes back with results. It runs on
infrastructure you control, with the models you configure.

## Layout

```
infinity/
├── web/         the browser UI (web/app) and its shared packages (web/packages)
├── engine/      the Python engine, package `omnigent`: API, sessions, memory, artifacts, helpers
├── host/        what ships into each person's host besides the engine: helper commands, deck export, pptx runtime
├── deployment/  the two Dockerfiles, the Compose file, setup scripts and engine config templates
└── docs/        one file per subject, and the decision records in docs/adr/
```

The engine's layer map and where-is table are in [`engine/README.md`](engine/README.md).

## Run it

Docker with the Compose plugin is the only prerequisite.

```sh
git clone <this repository> infinity && cd infinity
./deployment/setup.sh
open http://127.0.0.1:5173
```

`setup.sh` writes `.env`, fills its secrets, builds both images and starts the stack. Set
`ANTHROPIC_API_KEY`, or `LITELLM_BASE_URL` and `LITELLM_API_KEY`, in `.env` for models. Everything
else, including several people behind a sign-in broker, is in [docs/setup.md](docs/setup.md).

## Read next

[docs/README.md](docs/README.md) lists the pages in reading order. Contributing:
[CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).

## Licence

Apache-2.0. See [LICENSE](LICENSE), and [NOTICE](NOTICE) for third-party credits.
