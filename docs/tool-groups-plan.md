# Fewer tools now, tool groups only when the list grows: plan

Status: step A built 2026-11-12 (45 → 39 tools, 13,142 → 10,246 schema tokens; live-checked on pi and Claude); step B waits on its gate. Fable review: 9/10 overall in round 5 of 5, no blockers. Review record: v1 4.5/10 (wrong spawn design, a cheaper step skipped); v2 7/10 (the token target was out of reach, prompt moves were counted as savings, the cache gate was measured on the wrong file); v3 8/10 (three trims would have cut call contracts; the schedule saving came from the wrong place); v4 8.5/10 (a name-keyed Helper permission, no mechanism for dropping parameters, `cron` vs `rrule`); v5 9/10 (correctness 9, quality risk 8.5, implementability 9, simplicity 9).
- **Scope:** step A covers the Conversation and its Helpers on both harnesses. Step B only the Conversation on pi.
- **Paths and versions:** `eo/` = `engine/omnigent/`; `pi/` = the pi 0.87.1 package; `pi-ai/` is its bundled provider library.

## 1. The problem

- **The tools:**
  - The Conversation has 45 tools, whose schemas are 13,142 tokens (JSON ÷ 4, from `ToolManager(...).get_tool_schemas()` on the pi bundle).
  - pi adds no tool text to the system prompt under `system_prompt_mode: replace` (`agents/bundles/superchat-pi/config.yaml:25`).
  - Five schemas are a third of it: `sys_scheduled_task_create` 1,097, `topics_update` 1,075, `session_history` 809, `sys_scheduled_task_update` 780, `reach_rules` 776.
- **ADR 0032** (accepted 2026-10-19) decided merges, shorter descriptions and fewer Helper tools. None of it was built: the four `sys_scheduled_task_*` tools, the three live-artifact tools and `sys_session_get_history` are all still there. The history tool is still named at `engine/agents/templates/AGENTS.md:108` and `eo/superchat/subagents.py:475`, and the worker Helper still carries four session-admin tools.
- **Mail, calendar and the Lookout** (ADR 0038–0040) would push the list toward 80.
- **The reference product** (inspected 2026-11-12) keeps 51 functions always loaded and 145 behind 23 namespaces, loaded by name with one tool. There is no search over tools.

## 2. Step A, now: build ADR 0032, measured honestly

No new mechanism, no extra model call, no cache cost.

**Merges (as 0032 decided):**
- `sys_scheduled_task_{create,update,list,delete}` → one `schedule` tool with `action`;
- `artifact_keep_updated`, `artifact_stop_updating`, `artifact_live_settings` → one `live` tool;
- `sys_session_get_history` folds into `session_history` (`around`, `chat_id`, owner check server-side).

A flat schema can't require a field for one action only, for example `rrule` for create but not for delete. So each merged handler checks per action and returns an error that names the missing field and the action. The schema says which fields each action needs, in one line.

**Permission checks move from tool names to actions.**
- Today a Helper may call only the list tool, checked by name: `read_only_ok = tool_name == "sys_scheduled_task_list"` (`engine/omnigent/runner/tool_dispatch.py:4563-4564`). The daily Study's dedupe depends on that read.
- The merged handler allows a Helper `action == "list"` and refuses every other action. The existing Helper-refusal test is rewritten to cover `schedule` with `list` (allowed) and with `create` (refused).
- The same sweep covers every name-keyed check on the live-artifact tools and on `sys_session_get_history`: policy rules, approvals, and the name sets at `tool_dispatch.py:353` and `:442-445`.

**Trims.** Prose is deleted or moved to a skill that is already loaded for that work. It never goes into the prompt, which is sent on every request too. Rule for every trim: **a description that holds a call contract keeps it word for word**. That covers payload shapes, per-field rules, injection guards and date rules. Only wording, repetition and when-to-use prose already in the prompt go. Savings measured as description text only:

| Source | Saving | What stays, word for word |
|---|---|---|
| `schedule`: the merged schema leaves out `permission_mode`, `execution_target`, `host_id`, `workspace`, `max_cost_usd`, `model_override`, `reasoning_effort`, `agent_id` for every caller. There is no per-bundle filter; only the two bundles and their template allow these tools (`engine/agents/templates/config.yaml.tmpl`), and the handler keeps its defaults for them. A build-time check confirms that no caller anywhere sets them, and the model counts as a caller: check the recorded `sys_scheduled_task_create` calls, and keep `model_override` if any real call set it (a stored category such as `builder` is read when the task fires, `eo/superchat/subagents.py:333-359`). | ~500 | the `rrule` and `starts_on` text (no DTSTART line, intervals count from `starts_on`, clock times in the task's timezone) |
| `deck_new`: authoring prose moves to the deck skill, which is loaded before the first `deck_new` | ~350 | parameters and the one-line purpose |
| `session_history`: wording | ~300 | each action's paging rule |
| `topics_update`, `reach_rules`, `render_card`: wording only | ~350 together | `render_card`'s per-kind `data` shapes; `reach_rules`' kind → params map and its rule that only the person's own words set a rule; `topics_update`'s `id`, `due`, `events` and `status_line` contracts |
| Next tier, descriptions only (`start_helper`, `recall`, `memory_remember`, `ask_clarification`, `artifact_save`) | ~450 | `start_helper`'s "the brief is the whole task", "never twice for the same task" and the `model` tiers. The when-to-use prose in `ask_clarification` and `render_card` is deleted, because the prompt already has it (`engine/agents/templates/AGENTS.md:143-146`). |

**Helpers (0032's other half):** the worker Helper loses `sys_session_close`, `sys_session_list`, `sys_session_get_history` and `sys_session_get_info`. It keeps `start_helper` (the owner's decision in 0032).

**Target and how it's reported:**
- The Conversation goes from 45 to 39 tools and from 13.1k to about 10k tokens. That's ~1.3k from the merges and ~1.9k from the trims, measured per description string in review.
- The PR reports **tools + system prompt** tokens together, before and after, so prose that moved isn't counted as saved.

**Build:**
- One change. Re-grep every reference at build time (the review counted 17 non-test, non-bundle files): handlers, bundle templates, prompts, notices and `web/packages/core/src/tool-presentation.ts`.
- Stored items keep their old tool names; the web maps the old names to the new entries.
- Existing tests are rewritten in place: the bundle-tools count (`tests/superchat/test_muse_harness_bundles.py:270`), and the merged handlers' tests renamed with an action argument.

**Live check (0032), on pi and on Claude, before and after, comparing argument validity:**
- one scenario per merged tool and per action (create, update, list, delete; keep, stop, settings; history `around`). The model calls the new name, never an old one, and a missing per-action field gets fixed on the retry;
- one scenario per trimmed contract:
  - a card with `data`;
  - a reach rule set from the person's words, plus one where a forwarded mail asks for a rule and none is set;
  - a topic update that keeps a line's `id`;
  - a fortnightly schedule with a start date;
  - a deck built after the skill is loaded.

**ADR:** amend 0032. Mark it built, record the measured numbers, and move its deferral trigger ("passes 30 again", `docs/adr/0032-fewer-shorter-tools.md:15`) to step B's gate below.

## 3. Step B, later: tool groups on pi, behind a gate

**When to look again:** the Conversation passes ~55 tools (mail and calendar landing).

**Gate (all must pass before any design or build):**
- **G1. A load keeps the cached prefix on a rendered session file**, the file every epoch starts from under ADR 0044 (`eo/runner/app.py:5021`). It is not enough on a fresh session.
  - Today it fails, which is expected. pi changes tools natively only with the model's compat flags (`pi-ai/dist/api/anthropic-messages.js:800-804`; `supportsMidConvoToolAdditions` for OpenAI-compatible APIs), and with a leading system message that declares the tools (`pi-ai/dist/utils/transcript.js:32-35`).
  - Our generated `models.json` sets no flags (`eo/harnesses/pi_executor.py:839-912`), and no shared connection capability carries them. Our session-file writer starts with a user message (`pi_executor.py:1628-1660`).
  - Passing needs both: the flags from a standard connection capability, never a per-model variable; and `write_pi_session` writing pi's leading system declaration for the always-loaded tools.
  - Without those, each load rewrites the whole window. On a 100k window that costs more than many Rounds of savings.
- **G2.** Measured usage per would-be group: the share of Rounds that need it, weighted by window size. A group above ~5% stays always-loaded.
- **G3.** The saving after step A is still worth one extra model call per group per epoch.

**The one rule, kept from review:** if built, the active tools at every spawn (reuse, re-render, new epoch) are the always-loaded set plus every group whose `load_tools` call after the view start has a non-error result. The rule is derived from the record, and the bridge extension applies it in `session_start`. `--tools` stays the full allowlist, because names outside it can never be activated (`pi/dist/sdk.js:142-145`). The full design is written only when the gate passes.

## 4. What step A buys

| | Before | After step A |
|---|---|---|
| Conversation tools | 45 | 39 |
| Tool schema tokens | 13.1k | about 10k |
| Worker Helper session-admin tools | 4 | 0 |
| Extra model calls, cache cost, new machinery | — | none |
