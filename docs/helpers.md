# Helpers

How the assistant delegates work, how scheduled work runs, and what the activity panel shows, as
of 2026-10-19. The decisions are [ADR 0007](adr/0007-the-muse-delegates-multi-step-work.md) (the
assistant delegates multi-step work), [ADR 0005](adr/0005-proactive-work-is-scheduled-helper-runs.md)
(proactive work is scheduled helper runs) and
[ADR 0022](adr/0022-the-orchestrator-narrates-while-helpers-work.md) (the assistant narrates while
helpers work). Where the code lives is the "Helpers" row of
[`engine/README.md`](../engine/README.md).

## Helpers

A helper is a child session the assistant starts for one bounded job while the conversation goes
on. It runs in the same host as its parent and starts in the parent's working directory. The person
can open it from the activity panel and read every step, but cannot write in it.

The assistant starts one with `start_helper` (`superchat/helpers/tools.py`):

| Argument | Meaning |
|---|---|
| `task` | the whole brief; the helper does not see the conversation |
| `title` | 3 to 6 words the person would say |
| `model` | `builder` (the default, for writing), `light` (fetch-and-compare) or `foundation` (judgement): the person's pick for that category ([ADR 0027](adr/0027-super-chat-is-an-agentic-layer.md)); `fast` and `strong` are accepted as Light and Foundation for one release |
| `reasoning` | `low`, `medium` or `high`, or omitted |
| `files` | uploaded file ids |
| `project`, `path` | start in another Project's or file's folder; a `path` inside the workspace that does not exist yet is made |

The same task from the same chat within 10 seconds is treated as one hand-off. Several calls in one
step run helpers in parallel. Under [ADR 0029](adr/0029-a-round-is-recorded-and-the-harness-owns-the-words.md)
the helper's first message is the brief, then a facts block the engine appends: the folder, up to
five files in play, and the memory profile's preference lines that share words with the brief. It
never gets the whole profile. Its folder is stored on its session record before the brief is
posted, and that first message is what launches its harness process, so it starts in that folder on
both harnesses. It can read memory, `recall`, `recall_get` and `session_history` but not write
memory.

`message_helper` passes a correction or detail to a helper that is already working; it reads it at
its next step. If the helper finished by then, the message is refused rather than starting a second
task.

Limits:

- **Nesting.** Conversation, then helper, then one level of parts: a `worker` may start
  `subworker`s, which start nothing (`refuse_subagent_nesting` in `superchat/subagents.py`).
- **Concurrency.** 10 live helpers per conversation tree (`OMNIGENT_SUBAGENT_MAX_CONCURRENT`), plus
  each type's `max_sessions`. The count is read from the engine's child-session rows.
- **Delegation.** Judgement, not a limit: one deliverable that fits one turn is done in the
  conversation; work in another folder, long work and parallel work go to a helper. It is a line
  in the prompt and the engine refuses nothing for it
  ([ADR 0029](adr/0029-a-round-is-recorded-and-the-harness-owns-the-words.md)).
- **Approvals.** Sending, posting, deleting, spending, uploading and sharing in the person's name
  ask first, for helpers too (`superchat/approvals/`). A helper's ask is shown where the
  conversation's asks show (`GET /v1/me/asks`) and waits up to 7 days; after 120 seconds the parent
  is told the helper is blocked on the person.

## Pause

`POST /v1/sessions/{id}/helpers/pause` and `/resume` on the Conversation (body `{helper_ids?}`,
default every live helper for pause and every paused one for resume) answer `{paused: [ids]}` or
`{resumed: [ids]}` ([ADR 0027](adr/0027-super-chat-is-an-agentic-layer.md)). Its runner holds each
helper at its next tool call, built-in or the harness's own (`McpExecutionRegistry.hold` in
`runner/mcp_execution_registry.py`), and once it acknowledges, the helper gets the
`superchat.paused` label; a restarted runner restores the pause from it at the helper's next turn.
A held helper shows `session.status` `running` with `blocked_on:
"Paused"`. The Conversation's own turn is never paused. A helper left paused for 30 minutes is
stopped the way Stop stops it, and the Conversation gets one line: "<title> was paused for 30
minutes and stopped."

The Conversation reviews a helper's file against the ask with `artifact_outline` (a deck's slides,
a sheet's headers, or a text file's first 60 lines), offered to the Conversation only.

## Helper types

Each bundle declares its types in `engine/agents/templates/agents/<type>/config.yaml`; every type
runs on the bundle's harness. No type pins a model.

| Type | Started by | `max_sessions` | Tools |
|---|---|---|---|
| `worker` | `start_helper` from the assistant | 5 | the work toolset: `sys_os_*`, `artifact_save`, `artifact_list`, `deck_*` authoring, `files_*`, the live-update tools, `memory_search`, `memory_get`, `session_history`, `vault_fill`, plus `start_helper` for parts |
| `subworker` | `start_helper` from a `worker` | 5 | the worker set without `start_helper` |
| `notes` | the engine only, for the daily-note passes | 2 | `daily_note_get`, `daily_note_update`, `memory_*` (read, remember, forget), `session_history` |
| `editor` | the engine only, one per file, when its studio is first used (`POST /v1/artifacts/{id}/chat`, [ADR 0024](adr/0024-the-artifact-studio.md)) | none (outside helper caps) | `artifact_patch`, `artifact_save` (with `base_version`; a deck's save returns its `layout` check, an identical save makes no version), `artifact_list`, `deck_check`, `deck_export`, `files_*`, `sys_os_*`, `session_history`, `memory_search`, `memory_get`, `memory_remember`; never wakes the conversation, never listed as its helper |

## Scheduled runs

Proactive work is a scheduled task bound to the conversation whose fire starts a helper under it
(`server/scheduled/helper_fire.py`): gated on the person's proactivity and quiet hours
(`GET`/`PUT /v1/me/proactivity`), deferred while another proactive run is in flight, retried once
after a failure, and launched on the parent's runner. Its result comes back by the same path as any
helper's, so the assistant decides whether it is worth telling the person. The assistant manages
these tasks with the `sys_scheduled_task_*` tools; a helper can only list them.

Two kinds run without a request from the person:

- **Live page updates** ([artifacts.md](artifacts.md#live-pages)), one task per live page.
- **Daily-note passes** ([context.md](context.md#daily-notes-and-the-working-list)): ad-hoc
  `notes` helpers whose results never wake the conversation.

`GET /v1/me/system-jobs` lists the background jobs the engine runs on its own.

## The wake path

When a helper's turn ends, the runner records its status (`completed`, `failed` or `cancelled`; a
helper with live parts of its own waits for them) and posts a **wake notice** to the chat that
started it: `POST /v1/sessions/{parent}/events` with `is_system_notice: true`, carrying the
helper's final message (up to 4,000 characters, then a pointer to its history) and the names of the
files it saved (`format_subagent_wake_notice_with_result` in `superchat/subagents.py`, posted by
`_deliver_subagent_wake_post` in `runner/app.py`). The post is retried on temporary failures and,
failing that, after the next reconnect or turn.

If the parent is idle, the notice starts a turn, so the assistant speaks without the person
writing. If a parent turn is running, the notice is steered into it or runs next; a steered
result counts as read once the Conversation's session goes idle with nothing queued, and its
Round then settles ([ADR 0029](adr/0029-a-round-is-recorded-and-the-harness-owns-the-words.md)
amendments). The transcript hides the notice and shows the helper as a `helper` block
(`session_id`, `title`, `status`).

The notice asks for the hand-back as one or two plain sentences, then the card. An answer card
carries those sentences in `render_card`'s `message`, drawn above the card and used as the turn's
words when the turn wrote none; a terminal card without `message`, or chips, in a turn with no
words yet is sent back with `say_first`, at most twice per turn (`superchat/cards/handlers.py`).

Two more notice kinds, `progress` (one line per completed helper step) and `flag` (a decision or
surprise the person should hear now), come from ADR 0022; a progress notice wakes an idle parent
at most once every 30 seconds, a flag always. Their contract is in
[conversation.md](conversation.md#the-assistant-narrates-while-helpers-work).

## The activity panel

An activity is one piece of multi-step work, derived on read from sessions and items and never
stored (`superchat/activity/derive.py`). The panel shows one row per request: the turn's
background job and the file it made, with that file's later studio edits, sit under it (a studio
turn's `artifact.round_id` names the Round that created its file, `activityTree.ts`), and a
request whose job outlives its turn reads as running until the job ends, then shows the job's
outcome. It is either a conversation turn that did real work (at
least 3 steps, a work tool, or more than 60 seconds) or a whole helper task. Each has a title, a
one-line outcome, a status (`in_progress`, `done`, `failed`, `cancelled`), a source (`turn`,
`background`, `scheduled`, `housekeeping`) and plain-language steps. Titles are tidied from the
request or written by the model in the background, at most 8 per read. Daily-note passes fold into
one housekeeping row per local day.

Routes: `GET /v1/sessions/{id}/activities` (`limit` up to 100, `before`, `tz` for day grouping),
`GET /v1/sessions/{id}/activities/{activity_id}`, and `GET /v1/sessions/{id}/activities/stream`;
the family stream also sends `activities.changed`.

### How to register a tool

A step never shows a tool's raw name, only an icon, a plain title and an optional snippet, all from
`TOOL_PRESENTATION` in `web/packages/core/src/tool-presentation.ts`. When a tool is added to the
bundle's allow list, add an entry there with an icon key, a `title(args)` built only from the call's
arguments and a `snippet(output)`, or `tool-presentation.completeness.test.ts` fails. Map a new icon
key in `web/app/src/components/chrome/toolIcons.ts`.
