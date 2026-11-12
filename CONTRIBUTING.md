# Contributing to Superchat

Thanks for helping improve Superchat. Keep changes focused and testable.

## Run locally

Run the stack with Docker as in [docs/setup.md](docs/setup.md), then `pnpm install && pnpm dev` for
the web dev server against it (see [Builds](docs/setup.md#builds)). Engine tests run one file at a
time from `engine/` ([engine/README.md](engine/README.md)).

## Checks before you open a PR

| Command | When to run |
| --- | --- |
| `pnpm test` | Default. Units and properties for the web and the shared packages. No live model-provider calls. |
| `pnpm check` | TypeScript (`tsc`) across the monorepo. |
| `pnpm lint` | Biome lint and format check. |

CI runs `pnpm lint`, `pnpm check`, production builds and `pnpm test` on every PR.

## Adding a UI language

The web UI uses Lingui catalogs. To add a locale, register it in
`web/app/lingui.config.ts`, `web/app/src/lib/ui-locale.ts`, and
`web/app/src/lib/i18n.ts`, then run `pnpm --filter @superchat/web intl:extract`, fill the new
`web/app/src/locales/<locale>/messages.po` catalog, and validate it with
`pnpm --filter @superchat/web intl:compile`. Keep message IDs, placeholders, JSX markers, and
ICU plural branches intact; do not commit generated `*.js`/`*.mjs` catalog files. The web
build runs `lingui extract` first, so a new string never ships as a bare message id; still
commit the extracted catalog with the change that adds the string.

Update the locale unit tests for the new locale.

## Secrets and configuration

- **Never** commit `.env` files or secrets.
- **Never** paste API keys, tokens, or passwords in issues or PRs.
- Use placeholders in examples (`your-openrouter-key`, etc.).

Turns run on the engine; `pnpm dev` proxies to it at `OMNIGENT_URL`. Which secrets exist and which
never leave the engine: [docs/setup.md](docs/setup.md#secrets).

## Pull requests

- Keep PRs small and easy to review.
- Target the `main` branch.
- Describe why the change is needed, what changed, and **how you tested** (e.g. `pnpm test`, manual steps).
- Link related issues when applicable.

## Contact

See [SECURITY.md](SECURITY.md) for how to report vulnerabilities. For everything else, open an
issue.
