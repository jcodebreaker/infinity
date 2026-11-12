# The engine is the whole backend; Superchat is one client

Any website should be able to talk to Omnigent and get the whole product: the Muse, side chats, Helpers with live activity, decisions, files, memory and cards. Today a second site could not, because part of the behaviour lives in Superchat's API and adapters. The worst case is the Conversation itself:
- Superchat copies each Muse reply into its own database.
- A polling job mirrors messages the Muse sends on its own.
- Superchat's adapters turn raw engine items into chat blocks: one Helper row per `call_id`, cards, file cards, and dropping system notices.

Side chats already read from the engine, so the two paths already disagree.

We move every rule a client would otherwise have to copy into the engine, behind public routes and events. Superchat keeps only presentation: layout, wording and translation.

The first step is the transcript:
- `GET /v1/sessions/{id}/transcript` returns the Conversation as typed blocks (text, card, helper, file, secure entry, error with a code). Blocks are already de-duplicated, system notices are filtered out, and a side chat's copied context is left out.
- `GET /v1/sessions/{root}/family/stream` is one SSE stream for a Conversation and its side chats and Helpers. Events carry ids only (`message.done`, `chats.changed`, `activities.changed`, …), and clients refetch what changed.

Superchat then reads the Conversation the same way it reads side chats, and the reply copy, the mirror job, the block mapping and the polling loops are deleted.

Later steps follow the same rule:
- `/me/muse` and the chat's kind, root and project on the session, with bearer auth and a tenant claim.
- The decisions inbox and the Feed served by the engine.
- Error and outcome codes instead of English copy.
- Redaction applied by the engine.
- Generic labels for the host launcher.

## Considered options

- **Keep Superchat's API as the product backend and document it.** Rejected: the behaviour would then be split across two services in two languages, and the engine's own clients (CLI, Omnigent web) would never get it.
- **Ship the adapters as a client SDK.** Rejected: every client language would need its own copy, and a client-side copy cannot be the source of truth for a transcript the Muse also writes to on its own.
- **Push full payloads over the stream.** Rejected for now: an event that carries only ids, followed by a refetch, is simpler to get right across reconnects. Payloads can be added later behind the same event names.

## Consequences

- The engine gains a transcript projection (ported from Superchat's adapters) and a family event stream. Both are tested in the engine.
- Superchat's `OmnigentSession` run lifecycle, `mirror.ts`, `turnBlocks` and the side-chat polling are removed once Superchat reads from the engine.
- Wording stays with the client. The engine returns codes and data, and model-written titles stay as they are.
