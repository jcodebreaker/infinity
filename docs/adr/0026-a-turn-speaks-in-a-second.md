# A turn speaks in a second

> **Status:** Accepted and built 2026-10-17 (speak-first rule, warm Chromium per host, look as a record, bounded task events, policy evaluation and memory routes off the event loop with a loop-lag probe, titles as one proxy call, CLI pre-launch and parallel prefix, tool calls stored at start, Stop end to end, engine-side metrics and the scenario budget); first measured rerun pending the image rebuild. Sets the latency budget that [ADR 0022](0022-the-orchestrator-narrates-while-helpers-work.md) named and the first real turn missed; amends the deck rules of [ADR 0011](0011-a-deck-is-an-html-file-exported-in-the-host.md) (one warm browser per host) and the look rule of [ADR 0025](0025-act-by-default-tasks-have-state.md) (`look_from` is a record, never a gate). Amended 2026-10-19 (fix wave): pi pre-launches too, a thinking block counts as visible; see [Amendments](#amendments).

The first turn watched after ADR 0025 ("create some presentation about acme vs globex") took 182 seconds from send to the first word, on a warm host. The model wrote nothing before building; the deck tools launched a cold Chromium four times; the engine overrode the model's theme and forced a restyle; the engine's event loop stalled for seconds at a time; the Claude CLI booted on the first turn; the Stop control sent nothing. None of it was the model being slow. The reply itself streamed in under a second once it started.

## Budget

Measured from the engine's acceptance of the person's message, logged by the engine, failed by the scenario runner when over:

| What | Warm runner | Cold runner |
|---|---|---|
| First visible event (a word, a plan, a tool started, a thinking block opening) | under 1 s | under 10 s |
| First word | under 2 s | under 12 s |
| A six-slide deck, send to card | under 60 s | under 75 s |
| Stop takes effect (next tool boundary) and the pill settles | under 1 s | |

## Decision

- **Speak before the first tool call of any request that does work**, not only before a helper. The deck rules say so too; the "plan in your head" line goes. The executor emits a tool-started event the moment a tool call begins, so the person sees the work even when the model skips the sentence.
- **One warm Chromium per host.** The deck tools share one headless browser started at host boot and restarted on failure; a layout check never pays a cold launch. `deck_theme_set` restyles without a lint. A cancelled deck tool kills its helper process.
- **`look_from` is a record, never a gate.** An explicit `template` from the model is built. The engine chooses only when the model gave none, and says which it chose. Nothing the engine does forces a second call.
- **The engine's loop never stalls.** A loop-lag probe logs any lag over 250 ms with a stack sample; the default executor is sized explicitly; whatever blocked the loop during the deck build is found and moved off it.
- **Engine calls from a tool result are bounded.** A metadata call (a task event, a hint) has a 2 s budget and one retry, never blocks the tool, and tolerates late or out-of-order arrival on the server. A helper's turn makes no call whose result it throws away.
- **Cold start is paid before the person speaks.** The Claude CLI boots at runner session init; the gateway probe is skipped when the base URL is known; the prefix blocks are fetched in parallel.
- **Stop stops.** The web sends the interrupt even while a send is in flight and clears its own pending state; the engine answers whether it was delivered, cancels a deferred dispatch, and cascades to running helpers; the runner cancels the running tool at the registry instead of shielding it.
- **The metric is the person's wait.** Three engine log lines per turn: first visible event, first word, and "no text" when a turn ends silent, all from the message's `created_at`. The scenario runner records them and fails the budget.

## Considered options

- **Let the model stay silent and only show the strip.** Rejected: the strip is lit but empty for a minute; a sentence costs one second and says what is happening.
- **A fresh Chromium per check, but faster.** Rejected: a cold launch under load timed out at 20 s; a warm browser is the only launch that is free.
- **Kill the harness on Stop.** Rejected: the harness closes cleanly on interrupt already; the shield on tool execution is the bug.

## Update: no cheap opener (ADR 0027, 2026-10-17)

A Light-model opener was considered to reach the sub-second first word and rejected: Foundation speaks first, the strip and the session panel light at the moment of sending, and the first word lands at Foundation's own first token, two to three seconds warm.

## Amendments

**Amended 2026-10-19 (fix wave).**

- **Both harnesses pre-launch.** The runner launches pi's process at session init as it does the Claude CLI, keyed by the session (the empty pre-launch message now carries the session key, so Claude's first turn no longer launched a second CLI), and skips it when a turn already started during init; a prompt-less pi turn returns without waiting on the boot (`runner/app.py`, `harnesses/pi_executor.py`).
- **Thinking is visible.** The first-visible-event line also counts `response.reasoning.started`: both harnesses think before most answers, and the Working line is up from the turn's start (`server/routes/_sessions/turn_metrics.py`).
- **No 120 s stall on a closed connection.** The runner's engine client retires idle pooled connections after 2 s (uvicorn closes them at 5 s), and the MCP proxy retries a transport error once on a fresh connection before waiting for a reconnect (`cli_auth.py`, `runner/proxy_mcp_manager.py`).
- **The transcript draws first.** A transcript read waits at most 0.5 s overall for the runner's live queue, and the web shows a loading line until the first page arrives (`superchat/transcript/routes.py`, `web/app/src/pages/app/conversation/Transcript.tsx`).
- **A turn waits for the real host connection.** A launch or wake waits on the host registry's own connect signal, bounded, instead of polling the hosts row ([ADR 0004](0004-engine-owns-the-host.md) amendment).
