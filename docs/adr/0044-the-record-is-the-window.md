# 0044: The record is the window

> **Status:** Accepted 2026-11-11 by the owner (plan reviewed five times, 5 → 9 out of 10). Amends [0031](0031-a-new-session-is-assembled-from-state.md) (the rejected option "resume the harness's session file", and the indexing note at line 50) and narrows [0017](0017-the-working-list-is-tagged-and-the-journal-lives-in-the-host.md)'s turn indexing.

A restart is most cold starts (ADR 0033: 17 of 20). pi ran with `--no-session`, so its window lived only in process memory, and a restart rebuilt a small summary start and handed it over as one flattened text prompt. The record could not do better: the runner sent a prefix before each turn (time, gap line, profile, Projects, file handles) and never stored it, and a steer was stored when it arrived, not where pi read it. So no window could be rebuilt exactly.

## Decision

**The engine records everything the model saw, and rebuilds pi's window from the record.**

- **Record fields.** A turn-opening message stores the prefix blocks sent before it (`context_prefix`; `[]` when none, absent when not written). A steer stores where pi read it (`delivered_after_call_id`, or `turn_start`). Both are record-only: never in the items API, SSE, search or memory extraction.
- **Epoch block.** When the view start moves, the runner stores the block the window started from as a hidden `epoch_block` item and names it in a session label next to the view-start label. It is never recomputed.
- **One runner-only write path** (`PATCH /v1/sessions/{id}/items/{item_id}/data`) and a runner-only `record=1` read on the items list. Both need the runner's tunnel credential.
- **Renderer.** A pure function turns the epoch block and the items since the view start into typed messages, with the stored prefixes, steers in their delivered place, a synthetic result for an interrupted call, images inline only for the last two Rounds, and reasoning skipped.
- **pi loads a rendered session file.** The Conversation's pi spawns with `--session <file>` from host scratch space, never `--no-session`. The file is disposable. pi's own compaction stays off; the engine keeps the turn-start, size-only window swap (0031).
- **Respawn rule.** Reuse the file when its tail matches the record. Otherwise re-render when every turn has its prefix and the window is at most twice today's assembled start. Otherwise start a new epoch from the checkpoint, exactly as before. A lost file never costs more than before.

**Memory and bulk, decided with it:**

- During a turn the Assistant saves only explicit standing instructions and corrections, with no search first. Inferred facts are left to background upkeep.
- The profile and Projects go as changed lines to a warm process; the full block goes after any spawn.
- `session_history get` returns one stored item unclipped by call or item id. `files_get` and discovery lists cap at 16,000 characters, Helper notices at 4,000, each with the paging pointer that reaches the rest.
- One indexer with one cursor indexes every settled turn; recall still leaves out the current epoch.

## Considered options

- **pi resumes its own session file and compacts with a hook.** Rejected again: pi checks compaction after every tool batch, so it can cut mid-task, and a failed hook falls back to pi's own summarizer. The harness would own the context.
- **Merge every background memory job into one prompt.** Rejected in review: it loses upkeep's verify-and-classify step and each pass's own retries.
- **Adopt an off-the-shelf pi memory package.** About fifty exist; all are single-user coding-agent memory on local files, pi-only, some cloud-backed.
- **Pay to keep the provider cache warm, or prime it.** Rejected earlier: it spends tokens on guesses.

## Consequences

- Restarts and idle reaps keep the exact conversation when the file is still there, and re-render it when the window is small enough. A host replacement loses the file; large windows then start from the checkpoint as before.
- The Claude harness keeps its own history path until its SDK is shown to load the same render.
- The Assistant's shell can read its own window file. It holds what `session_history` already returns, plus the system prompt, which is in this repository.
- A local runner without a tunnel credential cannot write the record fields; its restarts take the new-epoch branch.
