# 0033: The context wave as built

> **Status:** Done 2026-10-20 on the train stack, pi and Claude. The record of what [0031](0031-a-new-session-is-assembled-from-state.md) became in code, why each part is the way it is, and the evidence that it works. 0031 stays the decision; this record is the build. Steps 0–1 are on `super-stable` (`92098d04b`); steps 2–6 are on `fix-wave-0029` (`3c1d6432f`), not pushed.

## Why this wave

A new harness session starts often, and size is rarely the reason: on the train host 17 of 20 Conversation cold starts came from restarts or harness switches. Before this wave, every such start replayed the past and often summarized it while the person waited:

- the one size rollover ran an Opus summarizer in line for 38 s, over input that was 42% result notices, 34% tool digests and 2% the person's words;
- the new session got the working list four times and the full Artifacts list on every turn;
- the kept tail was counted on raw items but shown as digests, so two thirds of its budget was never seen;
- cold first calls were 60% of the Conversation's input spend, and cache misses after an idle gap another 17%, with both harnesses on the 5-minute cache;
- bulk entered warm contexts and was re-read on every call: deck HTML through the shell, the deck kit (about 15k characters) from every `deck_new`.

The aim was a new session that feels continuous and costs what it needs: state compiled from stores the engine already keeps, not the past replayed.

## How we worked

- **One step at a time.** Each step shipped alone, was deployed to the train stack, then checked live on pi and then on Claude before the next. An old path was deleted only after the step's chain test passed on both harnesses.
- **Builders and review.** Opus builders wrote each step in their own worktree; every diff was reviewed before merge, and steps 4–5 also got an independent Opus review (one privacy blocker and eight should-fixes, all fixed before deploy).
- **Proof from model calls, not unit tests** (owner rule). Every claim below comes from the LLM call traces (request bodies, usage, cache reads and writes, tool calls), the runner and engine logs, or stored data. Tests were only updated or deleted in place; the count went down (211 → 190 in the rollover suites).
- **Deploys were guarded**: no deploy within 3 minutes of the owner using the stack, and each mistake became a check in the deploy script (disk floors, stale bundles, the pi `max_tokens` assert, and now a deploy lock).

## What was built, step by step

| Step | What changed | Why | Evidence (live, train) |
|---|---|---|---|
| 0 | `engine/scripts/handoff_probe.py`: replays a real Conversation or a scripted one on pi or Claude, hands off at chosen turns (`warm`, `single@N`, `chain` of five), asks probe questions, reads the traces | A handoff can only be judged against the same conversation without one | Arms differ only in handoffs; each session runs on its own throwaway host, never the person's |
| 0b | 1-hour prompt cache on both harnesses (`PI_CACHE_RETENTION=long`, `ENABLE_PROMPT_CACHING_1H=1`), one flag `OMNIGENT_PROMPT_CACHE_1H`, default on | pi's 5-minute TTL was confirmed in traces (hits ≤ 5.0 min, misses ≥ 5.4 min); a cold rewrite costs more than the 1-hour write premium | Requests carry `ttl: 1h`; 1-hour writes on both harnesses |
| 1 | Recent turns counted as rendered (whole Rounds, digests, margin); result notices clipped to their outcome plus 600 characters; the "use this result" boilerplate moved to a reminder on the wake turn only; kept tail 16k → 8k | The budget was spent on text the model never saw; boilerplate was stored and replayed forever | Longest notice 652–653 characters; boilerplate in stored items 0; one conversation copy per call; no tool call repeated (0 of 279 and 0 of 147 calls) |
| 1 fixes | A relaunched harness starts from the record (no stale per-process history); a Helper's Result is its final message only; an unreachable provider answers 503, not 400 | Found by the first step-1 run: two conversation copies after a reconnect, each tool call three times, narration in Results | Clean on the rerun |
| 2 | Files by handle: one line per file (title · kind · version · id · last change), most recent first, ≤ 20 lines, then "N more: artifact_list"; the full list only on a session's first turn, then only changed lines | The full list rode every turn | Traces: full block after each handoff, then "Files changed since the last message" lines only |
| 2 | Studio element reads: `artifact_outline` gains `element` (≤ 4k) and `range`; `element="theme"` reads a deck's theme tokens and own CSS; a chart element brings its chart script; the studio gets the selected element's source, and the file line and outline only when the version changed; prompts forbid shell reads of saved files | Jobs restyling a deck read 6–8k characters of HTML through the shell, because the theme and chart data had no other way in | Studio check (`scenarios.py --studio`, on a copy of a deck): one `artifact_patch`, no shell read, context ≈ 10k; "The file is unchanged" on the third turn |
| 2 | The deck kit is a skill (`skills/decks/SKILL.md`, read with `load_skill`); `deck_new` returns the record, or one layout's example | The kit came back from every `deck_new` call | `load_skill decks` once (4.2k); `deck_new` replies 0.5–2k |
| 2 | No native Claude Code skills in any Super Chat session: no bundle plugin, `Skill` disallowed | Claude Code offered its own `Skill` tool and a second copy of the deck skill, against [0006](0006-omnigent-owns-everything.md). The flag never worked at first: the spec parser turned `native_skills: false` into the string `"False"` | `Skill` offered on 0 calls (was 40 of 40); no skills reminder |
| 3 | Every new Super Chat session starts from one assembled block: the working list once, a line per settled Round out of view (hidden Rounds marked "scheduled"), the person's own words (secret-filtered), the recent turns in the second person, and a last line naming `recall` and `session_history`. Deterministic, no clock. A view-start label tells recall where the model's view begins, and the turns before it are indexed in the background | Replaying the runner's cache and summarizing at start made the person wait and sent the past several times | Block once per session, 0.1–2.5k tokens; first call after a handoff 22–26k, cache read on 5 of 5; view-start label on every handoff; runner log "assembled history"; first word no slower |
| 3 | Deleted: the catch-up at runner start, the blocking cold rollover, the blocking idle-return summary, the full replay, the `OMNIGENT_CONTEXT_ASSEMBLE` switch (−404 lines) | A restart bridges; it never summarizes | Runner logs show no catch-up and no checkpoint at start |
| 4 | The fold: a background summary on the owner's Light model, over the session's own provider connection, when the turn lines pass 2k tokens or the context nears the handoff. Input: the previous checkpoint, the person's words, turn lines, one line per failed tool call, final answers clipped to 600 characters; never tool results, HTML or notices. Stored only on a clean stop; a failure keeps the old checkpoint; never blocks a turn or drops the warm session | The old summarizer ran on the session's own model (Opus), in line, over the noisy record | Engine log: folds on `claude-haiku-5-5`, input 1.9–8.9k characters, "stop stop", anchor moved; the next block carries the new working list |
| 4 | The runner's memory sync writes every checkpoint's journal lines once; recall's backfill follows the anchor and keeps each turn's Project; the per-turn recall hint excludes by the view start | Found in review: lost journal lines, turns indexed without their Project, "earlier" hits that were still in view | Fixed before deploy |
| 5 | One handoff decision before each turn, size only: T = min(120k, 0.6 × window), and only when the context is also more than twice a fresh session. A handoff drops the warm session so the next turn assembles; a return, an expired cache or a model switch keeps it (owner, 2026-10-20). Deleted: the 200k cap and the 12-hour return drop; the expiry and model-switch handoffs, built first, were removed by that decision | A warm session is replaced only when it needs compacting; the 2× rule keeps a rewrite from costing as much as what it replaces | Size handoff at a forced 20k: runner log "handoff … size", next call assembles, answers 5/5. The 2× guard was added after this test showed endless handoffs when T is below a fresh session's size (a small-window model) |
| 6 | The prompt says what is in front of the model ("What is in front of you": the record at a session start, the current lines, the way back); the pointers to STATE.md and the journal for current state are gone | The working list now arrives in the block; the pointers sent the model to read files it already had | Checked in the final run (below) |

## The hint-free memory test

A scripted conversation (`engine/scripts/probe_cases/offsite.json`) plants facts, changes some of them later, pushes the early turns out of view with filler, then asks seven questions that give nothing away: headcount and budget; when and why; every hard requirement; which venue was ruled out and why; everything about the attendees; a trap ("Did I tell you which city the team is flying in from?" — never said); and the file, its version and last change, followed by an invitation email. Must and must-not patterns catch stale values (the old budget, the old dates) as hard fails, and every answer was also read and graded by hand.

| Arm | pi | Claude |
|---|---|---|
| No handoff | 7 of 7 right; one chose exact days inside the right week | 7 of 7 |
| Five handoffs, 2k kept tail, folds every few turns | 7 of 7 | 7 of 7 in substance; asked which venue was "second" when the page and its table disagreed |
| 75 s pause (the expiry handoff, since removed) | 7 of 7 | 7 of 7 |

No run gave a stale value as current, and no run invented the city in the trap. The emails asked everyone privately for accessibility and dietary needs instead of naming Priya or the vegans.

## Owner decisions this wave followed

- The fold runs on **Light**, in the engine; the session's model is only the fallback.
- Handoff at **120k**, capped at 0.6 of the window, by size only: a warm session is replaced only when it needs compacting (2026-10-20; this replaced "the cache-expiry handoff is the target, behind its flag").
- Budgets: recent turns 8k; the person's words 3k; turn lines 2k.
- The summarizer's journal lines are kept ([0017](0017-the-working-list-is-tagged-and-the-journal-lives-in-the-host.md)).
- The 1-hour cache came first.
- No new unit tests; proof from model-call traces.

## Deviations from 0031

- The fold writes no "Where we left off": it folds only the turns before the kept tail, so the newest turns stay raw and show the position.
- Privacy is enforced by routing: Light runs over the session's own provider connection. A server-wide `llm:` block for background work, when an operator sets one, wins.
- The size handoff needs the context to be over twice a fresh session as well as over T.
- The "assembled estimate" is the budget sum (24k plus the kept tail), not a count.
- No expiry, return or model-switch handoff (owner decision); the 12-hour return only adds the gap line.

## Known and deferred

- ~~Expiry handoff, firing branch.~~ Removed by owner decision (2026-10-20): a warm session is replaced only when it needs compacting.
- A scheduled run is folded into the person's turn before it; `/compact` folds at most 16k characters; the hard limit (95%) is a second drop point beside the handoff decision.
- pi jobs building a new deck still run a few small shell reads of its CSS (cut to 120–200 characters).
- The Conversation prompt is at 14,965 of the 15,000 characters its bundle test allows.
- Test sessions' files stay in the artifact store after cleanup and can surface in recall's "earlier work".
- [0032](0032-fewer-shorter-tools.md) (tool merges) is not built.

## Tools this wave added (dev only)

- `handoff_probe.py`: `run` (replay or `--script`, `--label`, arms), `cleanup` (keeps traces and runner logs, removes the throwaway host and its home), `report` (reads traces: cold starts, cache, block shape, notices, view start, scripted pass/fail).
- `scenarios.py --studio <deck id>`: the studio check on a copy of a deck.
- Deploy knobs for live tests, unset by default: `FOLD_TURN_LINES_TOKENS`, and the session labels `omnigent.context.rollover_keep_tokens` and `omnigent.context.rollover_at_tokens`.
