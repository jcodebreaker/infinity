# 0032: Fewer, shorter tools

> **Status:** Accepted 2026-10-19; split out of [0031](0031-a-new-session-is-assembled-from-state.md) after review. Requires the fx13 context hotfix (`session_history` already has `order: oldest|newest` and a 12k read cap). **Built 2026-11-12**, see [As built](#as-built). The plan that built it, and the gate for tool groups: [tool-groups-plan.md](../tool-groups-plan.md).

The tools block is 14.3k tokens of every Conversation call and the largest fixed part of a cold start. Several tools are one capability split in three or four, Helpers carry session-admin tools they never need, and some descriptions hold behavioural prose that belongs in the prompt or a skill.

## Decision

**Surgical.** No new tool machinery: merged tools reuse the existing handlers behind one name, and nothing is deferred.

- **Merges:** the four `sys_scheduled_task_*` tools become one `schedule` tool; the three live-artifact controls one `live` tool; `sys_session_get_history` folds into `session_history`, which gains `around` and `chat_id`. With `chat_id`, the handler checks the owner server-side and refuses a Helper reading another person's chat.
- **Helpers lose the six session-admin tools.** Worker Helpers keep `start_helper` (owner decision).
- **Descriptions shortened:** behavioural prose moves out of `render_card`, `deck_new` and `session_history` into the prompt or the deck skill.
- **Targets:** Conversation 41 → 34–35 tools, about 9k tokens; Helper about 26 tools, about 7k tokens.
- **Deferral** comes back only when the list passes 30 again, as an Omnigent tier like `load_skill`. Claude's tool search stays off (0029 first-word latency). *Superseded at build:* deferral is now looked at again only when the Conversation passes about 55 tools (mail and calendar landing), as tool groups on pi behind a cache gate: a group load must keep the cached prefix on a rendered session file, measured usage must keep each group under about 5% of Rounds, and the saving must still be worth one extra model call per group per epoch.

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

## As built

- **Merges.** `schedule` (`action`: create, list, update, delete) replaces the four `sys_scheduled_task_*` tools; `live` (`action`: keep, stop, settings) replaces `artifact_keep_updated`, `artifact_stop_updating` and `artifact_live_settings`. Each handler checks the fields its action needs and names the missing field and the action. `schedule` leaves out the agent, host, workspace, execution target, model, effort, permission mode and cost cap for every caller: no recorded call or code path set them, and the server keeps its defaults.
- **History.** `sys_session_get_history` is removed. `session_history` gains `around` (read from the turn that holds an item) and reads a Helper the chat started by `chat_id`; `get` with no id returns that chat's last reply, so a capped Result is read in full. The server lists the chat only when the owner matches, so a Helper cannot read another person's chat.
- **Permissions by action.** A Helper may call `schedule` with `list` only; every other action is refused.
- **Helpers.** The worker Helper loses `sys_session_list`, `sys_session_get_info`, `sys_session_close` and `sys_session_get_history`; it keeps `start_helper`, `sys_read_inbox` and `sys_cancel_task` for its own parts.
- **Descriptions.** When-to-use prose already in the prompt was deleted from `render_card`, `ask_clarification`, `start_helper`, `memory_remember` and `reach_rules`; the deck authoring rules moved from `deck_new` into the deck skill. Call contracts (payload shapes, per-field rules, injection guards, date rules) are unchanged.
- **Measured** (`ToolManager(...).get_tool_schemas()`, JSON ÷ 4, pi bundle): the Conversation went from 45 tools and 13,142 tokens to 39 tools and 10,246 tokens; the worker Helper from 34 tools and 8,267 tokens to 30 tools and 6,566 tokens. The Conversation prompt went from 17,280 to 17,273 characters; the deck skill, loaded only for a deck, from 4,240 to 4,643.
- Stored items keep the old tool names, so the web keeps their presentation next to the new names.
