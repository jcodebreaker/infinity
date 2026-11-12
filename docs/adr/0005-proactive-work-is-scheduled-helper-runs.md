# Proactive work is scheduled Helper runs

The Muse's proactive features (followed topics and the Feed, Goals, check-ins, Ideas, Routines) ran on Superchat's old agent backend with their own data, jobs and tools. We rebuilt them on the engine as one pattern: a scheduled task on the engine server whose fire starts a Helper as a child of the person's Conversation, does the work inside the Muse's host, and reports back through the same path any Helper's result uses, so the Muse decides whether it is worth telling the person. All state (schedules, Goal plans, results, preferences such as quiet hours) lives on the engine server; the host only does the work, so losing it costs one run, which is retried, never data.

## Considered options

- **Port Superchat's own Goal and Feed engines onto the new backend.** Rejected: it keeps a second scheduler, a second Goal store and a second delivery path beside the engine's, which is the two-sources-of-truth problem ADR 0004 removed for the host.
- **Free-standing scheduled sessions (the engine's default fire).** Rejected: each fire becomes a session the person never sees, so results need a separate delivery channel; binding the fire to the Conversation as a Helper reuses the existing one.

## Consequences

- The engine gains generic pieces any product can use: a parent binding on scheduled tasks, per-person gating (quiet hours, one proactive run at a time, a proactivity level), retry after a failed fire, and later an Objective store for Goals.
- Superchat's Pi-era goal, feed and check-in code and its background jobs are deleted once each feature has moved.
