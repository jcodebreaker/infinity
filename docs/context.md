# Context and memory

What the assistant sees on each turn, how a year-long conversation stays inside the model's window,
and what it remembers, as of 2026-10-20. The code paths are the "Memory and recall" row of
[`engine/README.md`](../engine/README.md); paths below are relative to `engine/omnigent/`. The
decisions are [ADR 0006](adr/0006-omnigent-owns-everything.md) (the engine owns context, the harness
only runs the loop), [ADR 0016](adr/0016-recall-is-a-bounded-hint-and-a-deep-pull-over-one-index.md)
(recall) and [ADR 0017](adr/0017-the-working-list-is-tagged-and-the-journal-lives-in-the-host.md)
(the working list and the journal).

## What the assistant sees each turn

In the order the model reads it:

1. **System prompt.** The bundle's `AGENTS.md` (see [architecture.md](architecture.md#where-the-system-prompt-lives)),
   framework instructions (the web-search line only for an agent that declares a `web_search` or
   `web_fetch` tool, `runtime/prompt.py`), and on `claude-sdk` a tool-naming note. Fixed for the
   life of a warm harness session. The model and harness are not in it (they go in the per-turn
   prefix), so a model change keeps a warm Claude client (`set_model`) and the pre-launched harness
   matches the first turn.
2. **Tool definitions.** What the bundle's `tools.allow` list leaves.
3. **History.** Either the harness's warm session, or, in a new session, one assembled block
   followed by the newest messages as stored (ADR 0031, "Rollover" below): the checkpoint's working
   list once, a line per settled turn since the checkpoint that is not in the tail, the person's
   words from those turns, and the newest whole turns in the second person, with each tool call
   one "→ outcome" line (`context/tool_digest.py`). Code only, no model call.
4. **The per-turn prefix.** Built fresh by `superchat/prompt_prefix.py` (`turn_prefix_blocks`) and
   put in front of the person's latest message, for the model only; never stored, so it never
   reaches the transcript, a summary, search or memory upkeep. Read top to bottom:
   - on a message that answers a question card, the pick line first: `They picked Pitch Deck.
     Continue the task; do not plan it again.`
     ([ADR 0025](adr/0025-act-by-default-tasks-have-state.md));
   - the person's local time (from the timezone in `GET /v1/me/proactivity`, UTC if unknown), and
     one gap line on the first message back from an idle break;
   - the Setup line: `[Setup: you run on the model <model> through the <pi | Claude Agent SDK>
     harness. Earlier messages may describe another setup: go by this one.]`, sent once to each
     warm harness session (again after a cold start, a model change or a change of instructions or
     tools), like the memory profile (`prompt_prefix.setup_line`);
   - after a change of model or harness, one line: `[Since the last message the setup changed:
     was pi with <model>, now Claude Agent SDK with <model>. Earlier messages may describe the old
     setup; go by the current one.]` (the runner remembers the last setup per Conversation in
     memory, so a runner restart forgets it);
   - feature notes, such as "the person edited this file by hand" (see
     [artifacts.md](artifacts.md#write-back));
   - the Rounds still working, one line each, read from the Round record: a Round counts while
     a Helper it started still runs, so a finished or failed one never lingers (`Open task: "a
     deck on Acme vs Globex" · helper "Acme vs Globex deck" · next: wait for the helper`). The Round the
     message itself joined (a status question asked while its job runs) is listed too while its
     Helper runs, with what the work is doing now and its finished steps (`· now: slides.html ·
     steps done: 4`, `tasks/service.py`);
   - the Projects list: one line per `PROJECT.md` card under `~/workspace/projects/`, the 15 most
     recent plus the open one;
   - the memory profile (below);
   - attached files' text in an `<attachment_context>` block, framed as data.
5. **The person's message**, as stored, with any image blocks.

The **context brief** is not in the prefix: it is the result of the Conversation's `context_brief`
tool ([ADR 0027](adr/0027-super-chat-is-an-agentic-layer.md), `superchat/context_brief/`). Context
is pulled (ADR 0029): nothing builds it when a message arrives, and the prompt only says what it
holds and when it is worth a look. A call builds it for the person's latest message (a runtime
notice is not one) and keeps it per session and message until the turn ends or two minutes pass, so
a second call gets the same text; a call that waits past the budget gets an empty brief. It reads,
in parallel within 1.5 seconds, what we made before (artifact groups whose name or title shares the
message's subject words, newest version, who saved it and when), the Conversation's Rounds still
working (what was asked and the Helpers still running on it; a settled Round is in the journal
instead), earlier work (the recall pull with up to three queries written from the message, as at
most six dated pointers; none with recall off), the top three matching files with their page count,
and the working list's `p-` lines on the subject (the memory profile is only pointed to). A part
that fails or is late is left out. It renders as plain lines under four headings, at most 40 lines
and 2,500 characters, fenced as data. Helpers and a file's studio never get it.

The memory profile and Projects blocks go in full on a cold start or when they changed; otherwise
each is one line ("Standing memory unchanged since the last message."). The prefix reads share one
budget (0.5 s, 5 s with the person's files); a profile or Projects list not read in time is
replaced by one line saying so and how to get it (`memory_search`, or listing
`~/workspace/projects/`), so the model never answers as if there were none. The per-turn prefix exists
because a harness's system prompt is frozen per warm session, and changing it would force a cold
start and break prompt caching. Any other failure building a block drops that block; no block can
fail a turn. Helpers carry the same `superside-chat` label and get the same prefix.

## Prompt cache

Both harnesses ask for a 1-hour prompt cache instead of the default 5 minutes
(`OMNIGENT_PROMPT_CACHE_1H`, on unless `0`; `PROMPT_CACHE_1H` in the deployment `.env`). The runner
sets `PI_CACHE_RETENTION=long` for pi (a `ttl: "1h"` on all its cache breakpoints) and
`ENABLE_PROMPT_CACHING_1H=1` for Claude Code (`harnesses/agent_env.py`, `prompt_cache_1h`). A
1-hour write costs 2x an input token instead of 1.25x, but only on new tokens; a read stays 0.1x.
The lifetime runs from the start of the last request that read the cache, and every read
refreshes it, so a person who comes back within the hour keeps the warm context instead of a
rewrite of the whole thing (ADR 0031, step 0b). Check it in a model-call trace: the request's
`cache_control` carries `"ttl": "1h"`, the usage splits writes into `ephemeral_1h_input_tokens`,
and the first call after a 10-minute gap reads the cache (`engine/scripts/handoff_probe.py
numbers` prints the TTLs asked and hits by gap).

## Rollover

The engine folds older turns into a **checkpoint** (the tagged working list and journal lines) and
a new harness session starts from that checkpoint plus the record after it, never from a replay.
The full history stays stored: the person sees every message and the assistant can read any of it
with `session_history` or `recall`. The harness's own compaction is switched off
(`DISABLE_AUTO_COMPACT` on `claude-sdk`, `compaction.enabled=false` on `pi`) so two compactors never
fight. Decisions: [ADR 0031](adr/0031-a-new-session-is-assembled-from-state.md).

**A new session is assembled.** Every new session (a runner or host restart, a harness switch, a
dropped warm client, a handoff) goes through the history load (`runner/app.py`,
`_load_history_as_input`), which renders one block (`context/rollover.py`, `assemble_block`): the
checkpoint's working list once, a line per settled turn since the anchor that is not in the tail
(hidden Rounds marked "(scheduled)"; a Helper or studio gets the same line from its transcript), the
person's words from those turns (3,000 tokens), the newest whole turns in the second person (8,000
tokens as rendered), then the newest messages as stored. It writes the view start label
(`omnigent.context.view_start`) that recall's current epoch reads, and indexes the turns before it
in the background. A restart bridges and never summarizes.

**The fold** (`superchat/rollover.py`, `fold_session` and `roll_over_session`) runs in the engine,
scheduled after each superside-chat turn beside memory upkeep (`server/memory_upkeep.py`,
`SessionFolds`), in the background: nothing ever waits on it, and it never drops a warm client.

| | |
|---|---|
| Trigger | the settled-turn lines a new session would list pass 2,000 tokens (`OMNIGENT_FOLD_TURN_LINES_TOKENS`), or the warm context reaches `fold_at = T − min(32k, T/4)`; at most one fold per session at a time, between turns |
| Model | the owner's Light model (Settings, Models) for the session's harness, over the session's own provider connection (its inference snapshot, `resolve_session_llm`), so a Conversation's words never reach another provider; keys never reach the host. With `OMNIGENT_FOLD_ON_LIGHT=0`, no Light model, or after two failed Light calls in a row, the session's own model over that connection |
| Window | the turns since the anchor before the kept tail, oldest first, at most 16,000 characters of rendered turns; the anchor lands after the last one folded, so the newest turns stay raw after it and the block's turn lines shrink |
| Input | the previous checkpoint, then per turn: the person's words, its turn line, one line per failed tool call (tool name and the error's first line), and the final answer clipped to 600 characters. No tool results, HTML or notices. Secrets are withheld as in the block. The instruction is fixed text (the date rides the input), so it is byte-identical across folds |
| Output | an anchored merge of the working list (only entries the turns touch change; an approach that failed is a `d-` entry starting "tried and failed:"), and 3 to 10 dated journal lines. No "where we left off" paragraph: the block shows the newest turns instead |
| Accepted | unless the reply was cut off, called a tool or was filtered; a reply with no tagged entries leaves the working list as it was and still moves the anchor; a stale anchor is refused. On any failure or timeout the previous checkpoint stays and a later turn tries again |

Code then reconciles the rewrite against the previous list (`context/state_file.py`, `reconcile`):
it applies pending `state_close` calls, puts back word for word any entry the rewrite lost (counted
as a repair, never a rejection), carries missing dates forward, cuts entries over 400 characters,
fits the list into 4,000 tokens by evicting closed and merged entries first and open preferences
last (evicted lines go to the journal), and marks stale entries. A turn that ran while the fold was
written is not lost: the checkpoint covers the record only up to its anchor.

**Handoff.** A warm session is replaced only when it needs compacting (ADR 0031, owner decision
4): the warm client is dropped before a turn, so that turn's history load assembles, at one
decision point (`superchat/rollover.py`, `handoff_reason`; `runner/app.py`,
`_superside_chat_handoff`). A return after a break, an expired prompt cache and a model switch keep
the warm session (Claude switches with `set_model`; pi respawns on a switch anyway). The first
message after 12 hours away (`OMNIGENT_ROLLOVER_IDLE_REFRESH_SECONDS`, from the newest stored item)
gets a gap line; it is not a handoff.

| Handoff | Rule |
|---|---|
| Size | the context the last turn reported (completed, stopped or failed) reaches T = `min(120k, 0.6 × window)`; 100,000 when the window is unknown; an explicit `omnigent.context.rollover_at_tokens` label sets it (`resolve_rollover_threshold`); and the context is over twice what a new session costs (`assembled_estimate`: 24,000 tokens plus the kept tail), so a small window never hands off every turn (logged once; the hard limit applies). Right after a handoff the old reading is ignored |
| Hard limit | the last known fill is 95% of the window or more: the turn starts from the checkpoint plus the newest whole turns that fit |

A runner that holds no warm history for a session (after a restart) has nothing to hand off: the
load assembles anyway.

| Other | Rule |
|---|---|
| Helper turn ceiling | inside one Helper turn (not the Conversation, not a studio), a model call's context reaches 300,000 tokens (`OMNIGENT_HELPER_TURN_CONTEXT_CEILING`): the job is told once to save its work and end the turn with what is done and left; at 1.25 times the ceiling the turn is stopped (`runtime/harnesses/_executor_adapter.py`) |
| Manual | `/compact` in the runner runs the same fold, then drops the warm client |
| Reset | `POST /v1/sessions/{id}/reset`: a checkpoint with no summary |

After a checkpoint is stored, memory upkeep is scheduled for the owner. The runner copies its
working list and journal lines into the host (below) at the next turn's memory sync, every
checkpoint stored since the last one it wrote, and recall's backfill indexes the turns up to the
new anchor with their Project. The context brief's earlier work leaves out the turns after the
view start, as `recall` does.

## Memory and upkeep

Long-term memory is a set of **claims** per person (table `memory_claims`, store in
`stores/memory_store/`, service in `memory/service.py`). Memory belongs to the person: the
conversation and every helper read the same set, and the owner is always resolved from the calling
session on the server, never from tool arguments.

A claim is one sentence with a `kind` (`preference`, `instruction`, `fact`, `decision`, `person`,
`project`, `working_style`, `commitment`), the person's exact quote, evidence links, `stated` or
`inferred` explicitness (confidence 0.9 or 0.4 to start), a reinforcement count, an optional
`valid_until` past which it reads as expired, and a status (`active`, `superseded`, `expired`,
`forgotten`). A claim the person edited is `person_authored` and is never overwritten: a
replacement is added beside it. Search is a txtai index (`memory/index.py`) over active claims,
hybrid BM25 plus embeddings when the embeddings model (`OMNIGENT_MEMORY_EMBEDDINGS_MODEL`, default
`openai/text-embedding-3-small`) has a key, keyword-only otherwise. Without the engine's `memory`
extra the memory routes are not mounted and the tools say memory is not configured; the engine
image installs it.

Claims are written two ways:

- **By the model, right away**, with `memory_remember` (a near-duplicate reinforces instead of
  adding), and removed with `memory_forget` (plan first, then confirm). `memory_search`,
  `memory_get` and `memory_explain` read. The rules for what is worth saving are in the system
  prompt.
- **By upkeep, in the background** (`memory/upkeep.py`, scheduled by `server/memory_upkeep.py`):
  after each fold of a conversation, and on an hourly sweep of recently active owners, at most
  4 owners at a time and one run per person. Only the person's own words since the last successful
  run are evidence; helper sessions, scheduled runs and system notices never are. Fewer than 3
  substantive new messages skips the run. What is worth keeping is one policy (`MEMORY_POLICY`,
  shared with the daily note's pass): who the person is, what they work on (recurring topics,
  audiences, projects) and how they like work done; never a one-off task instruction, a choice the
  assistant or a helper made, a question about the assistant, or a secret. The person's messages
  from the 14 days before the window are shown beside it, so a pattern across runs is visible; a
  claim cites a new message, and an inferred pattern may list verified `repeats` (more evidence
  links), each counting as one +0.2 reinforcement, so a pattern seen three times enters the profile
  at once. Each candidate must quote the cited message verbatim, stay under 500 characters, and not
  look like a secret or a one-off request. A similar existing claim is reinforced (+0.05 stated,
  +0.2 inferred), kept beside, or superseded only when the model names which claim. An extraction
  that times out or is not JSON fails the run (the window is read again next time). Upkeep runs on
  the owner's Light model. Each run's outcome in plain words (`describe_upkeep_run`) is shown on
  the Automations System row and returned with `memory_search` (`memory_upkeep`), so the assistant
  explains its memory from the runs, not from guesses.

The **memory profile** (`MemoryService.profile`) is what goes in front of every turn and every new
helper brief: active claims of every kind except `person`, with confidence 0.75 or more (a single
inferred claim stays out until it is confirmed), grouped by kind, newest-reinforced first,
`decision` claims dropped after 30 days without reinforcement, capped at about 6,000 characters,
between `[Standing memory about the user ...]` and `[End of standing memory]`. The person reads and
edits claims through `/v1/sessions/{id}/memory/...` (owner only).

## Recall

Finding earlier work is split by cost ([ADR 0016](adr/0016-recall-is-a-bounded-hint-and-a-deep-pull-over-one-index.md)):

- **The deep pull.** The `recall` tool (`superchat/knowledge/recall.py`) takes up to three
  `queries` plus optional `project`, `file`, `chat`, `since` and `until` filters, and searches four
  sources in one fused ranking: `files`, `journal`, `conversation` (retired turns) and `memory`
  (claims, fetched from the engine). Each sub-query runs BM25 and vectors; hits are fused with
  reciprocal rank fusion; a conversation hit loses half its score every 90 days, never all of it;
  near-duplicates collapse into the newest. Every hit is dated and carries an id for
  `recall_get`: `i_<item>` (a turn), `ep_<passage>` (a journal passage), `f_<file>@<page>`,
  `c_<claim>`. `session_history` still reads exact earlier items of the chat.
- **Recall in the context brief.** Nothing from recall is in the per-turn prefix (ADR 0027); the
  `context_brief` tool carries it. Unless the person turns recall off (`PUT /v1/me/recall-hint`
  with `{"recall_hint": "off"}`; on by default), code writes up to three queries from the message:
  the message, the message with the open working-list lines, and its paths, dates, numbers and
  names (`superchat/recall_hint/hint.py`, `turn_plan`). No model call: the Assistant calls the
  tool on purpose, and `recall` takes its own queries and dates. Turns still in the window are
  excluded, and nothing is listed when the best score is under the floor.

## The knowledge index in the host

The person's files and their index live in the host
([ADR 0014](adr/0014-files-live-in-the-host.md)). The index is SQLite with FTS5 (BM25) and
sqlite-vec on the home volume at `~/.superchat/knowledge`, kept by one background thread
(`superchat/knowledge/indexer.py`, `runtime.py`). It covers `~/workspace/your_files/`,
`~/workspace/memory/` and each `goals/<goal>/files/` folder, plus the `conversation` source: one
passage per turn, written when a new session leaves it out of its view or a checkpoint's anchor
passes it (`runner/memory/sync.py`), with only
the person's own words and the assistant's reply, never the prefix blocks.

A file is keyword-searchable as soon as it is parsed into passages, then embedded through the
engine's model proxy so no key enters the host; an owner without an embedding connection gets
keyword search with the reason. The model reads it with the `files_*` tools (`files_search`,
`files_vsearch`, `files_query`, `files_get`, `files_multi_get`, `files_read_page`,
`files_status`) and `recall`. The engine holds no file data: its `/v1/sessions/{id}/knowledge/*`
routes relay to the host.

An attached PDF, text, Markdown, CSV or XLSX file under `your_files/uploads/` is read into the
`<attachment_context>` block of its message (up to 8 files and 48,000 characters); one that is too
long becomes a one-line manifest pointing at `files_query` and `files_get`.

## Daily notes and the working list

- **The working list** is the tagged list inside each checkpoint (`context/state_file.py`). Every
  task, decision, open loop and preference is one line, `- [t-launch-budget · 2026-10-09] text`,
  with kinds `t-` topic, `d-` decision, `o-` open loop and `p-` preference. The tag is the entry's
  identity and the date when it was last touched. An open entry other than a preference untouched
  for 30 days renders ` · stale?`, a rendering, never stored. The assistant closes a wrong entry
  with `state_close`, applied at the next checkpoint.
- **The journal and `STATE.md`.** After each checkpoint (at the next turn, for one the engine's fold
  stored) the runner appends the journal lines and the
  evicted entries to `~/workspace/memory/journal/YYYY-MM-DD.md` and writes the working list to
  `~/workspace/memory/STATE.md` (`runner/memory/files.py`, `sync.py`). Each settled Round adds
  one entry (ask, outcome, files, Helpers, folder, duration) and a delivered hand edit a line.
  Each line names its chat and, written by code, what the work touched (files, project). The folder is a projection: nothing in the engine reads it back as
  state, it is rebuilt from the stored checkpoints when missing, and the index makes it searchable.
- **Daily notes** (`superchat/daily_notes/`): one per person per local day, stored in the engine
  (`/v1/me/daily-notes`), with sections for what was talked about, decisions, promises and open
  loops. A section the person edited is only appended to. They are kept by background `notes`
  helpers whose results never wake the conversation (`quiet_moment.py`): a quiet-moment pass when
  the conversation has been idle for 20 minutes (`OMNIGENT_QUIET_MOMENT_IDLE_SECONDS`) after at
  least 3 messages, at most 4 a day; a final pass in the local evening; then a nightly reflection
  ("Dreaming", which may update `working_style` claims) and a People pass. Nothing runs while the
  person's proactivity is off or in their quiet hours. The day's note and reflection are also
  written into that day's journal file.
