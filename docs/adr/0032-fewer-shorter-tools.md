# 0032: Fewer, shorter tools

> **Status:** Accepted 2026-10-19; split out of [0031](0031-a-new-session-is-assembled-from-state.md) after review. Requires the fx13 context hotfix (`session_history` already has `order: oldest|newest` and a 12k read cap).

The tools block is 14.3k tokens of every Conversation call and the largest fixed part of a cold start. Several tools are one capability split in three or four, Helpers carry session-admin tools they never need, and some descriptions hold behavioural prose that belongs in the prompt or a skill.

## Decision

**Surgical.** No new tool machinery: merged tools reuse the existing handlers behind one name, and nothing is deferred.

- **Merges:** the four `sys_scheduled_task_*` tools become one `schedule` tool; the three live-artifact controls one `live` tool; `sys_session_get_history` folds into `session_history`, which gains `around` and `chat_id`. With `chat_id`, the handler checks the owner server-side and refuses a Helper reading another person's chat.
- **Helpers lose the six session-admin tools.** Worker Helpers keep `start_helper` (owner decision).
- **Descriptions shortened:** behavioural prose moves out of `render_card`, `deck_new` and `session_history` into the prompt or the deck skill.
- **Targets:** Conversation 41 → 34–35 tools, about 9k tokens; Helper about 26 tools, about 7k tokens.
- **Deferral** comes back only when the list passes 30 again, as an Omnigent tier like `load_skill`. Claude's tool search stays off (0029 first-word latency).

**Scope.** About 21 non-test files name a merged tool: handlers, bundle templates, prompts, result notices (`superchat/subagents.py` still tells the model to call `sys_session_get_history`) and the web's `web/packages/core/src/tool-presentation.ts`. All are renamed in the same change. Stored items keep their old tool names forever, so the web presentation keeps the old names mapped to the new presentation.

## Considered options

- **Keep the tools and only shorten descriptions.** Rejected: the merges are most of the saving.
- **Defer tools now.** Rejected until the list grows again.

## Measuring success

A live check on pi and on Claude, no unit tests:
- the tools block's size in the model-call trace, and its hash stable across a session and across a handoff;
- the tools hash equal across harness restarts within the 1-hour cache lifetime (0031 step 0b), shown by `cache_read_input_tokens ≥ tools + system` on the first call after a restart. This makes step 0b a regression guard;
- a scenario per merged tool (schedule a task, toggle a live artifact, read history `around` an item) where the model calls the new name once and never an old one;
- an old conversation still shows its old tool calls with the right presentation in Activity.

## Build plan

One change, after 0031 step 2: rename the handlers, update every reference in the same commit, delete the old tool registrations, keep the web mapping. Rollback is a revert of that commit.
