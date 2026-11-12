# No app server: the web talks to the engine

Superchat ran a Node app server (API, background worker and a web server) between the browser and the engine. By [0009](0009-the-engine-is-the-whole-backend.md) the engine already owned the Conversation, memory, files, approvals and scheduled work, so the app server had shrunk to sign-in, accounts and a thin pass-through. It was a second service, a second database schema and a second language to run for almost no behaviour.

We delete it. The deployment is Postgres, the engine and one host per person. The engine serves the built web and the published apps (`/apps/<slug>`), and the web calls the engine's REST API on its own origin.

## Decision

- `apps/api`, `apps/worker`, `packages/db` and the Packages only they used (auth, testkit, adapter-kit, logging, and the Node-only parts of adapters, core and chat-ui) are removed. `@superchat/adapters` keeps only the engine client the browser uses (`omnigent/browser`).
- The `app` Compose service and its image are removed. The `omnigent` service publishes the web on `SUPERCHAT_WEB_PORT` (5173) and the engine API on `SUPERCHAT_ENGINE_PORT` (8012).

## Consequences

- **Identity comes from an SSO broker header.** The engine trusts an identity header only alongside a shared secret. Locally, with `OMNIGENT_LOCAL_SINGLE_USER=1`, a request with no identity is the one local person. There is no sign-up, no password and no Superchat account table.
- **Chat history starts fresh.** The app database held a copy of conversations the engine now serves itself; it is not migrated.
- **Dropped with the app server:** messaging connectors (Slack, Telegram, WhatsApp, email and the like), text-to-speech, Composio and Pipedream integrations, and groups. They can return as engine features.
- **The mobile and desktop apps are removed**, with their release workflows and e2e suites. Git history keeps them. The web is the one client.
- **A published app's `org` audience means any signed-in person on the engine.** There is no separate organisation model in front of the engine.
- Postgres has one schema, `engine`. Existing volumes keep an unused `app` schema until they are recreated.

## Considered options

- **Keep the app server for sign-in only.** Rejected: identity is already a header the engine accepts from a broker, so a service that only forwards calls adds a failure point and an image to build.
- **Keep mobile and desktop on a thin API.** Rejected: they depended on the removed API, and maintaining three clients was not worth it before the web is finished.
