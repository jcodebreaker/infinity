# A Round is recorded and the harness owns the words

> **Status:** Accepted 2026-10-18. Supersedes ADR 0028, which was built on branch `wave4` and never merged. Amends [ADR 0007](0007-the-muse-delegates-multi-step-work.md) (the step limit goes; delegation is judgement), [ADR 0027](0027-super-chat-is-an-agentic-layer.md) (context is pulled, not ordered; the evidence-ticked plan goes), [ADR 0023](0023-the-plan-is-visible-and-the-work-is-alive.md) (the strip shows facts, not a plan) and [ADR 0022](0022-the-orchestrator-narrates-while-helpers-work.md) (the engine stops hiding and writing the Assistant's words). Amended 2026-10-19 (fix wave): Round settlement, delivery, card words and a bounded `say_first`, adapter turn ends; see [Amendments](#amendments).

ADR 0027 had the engine tick a plan from evidence, and ADR 0028 replaced its tables with a `steps` tool, a ledger that credits every call to the active step, a refusal of `start_helper` while no plan exists, and an engine-written hand-back when a wake turn said nothing. In use, each piece became a place where the engine guessed: which request a wake turn belonged to by matching titles, which text was a working note by counting words, which step a call served, and what the Assistant meant to say when it was silent. Each guess could be wrong in a way the person saw: a strip that relit, a stopped run that read done, a reply hidden because it was short, a hand-back the model never wrote. What was missing was a record of what happened to one message from the person, written as it happened, and a view of it that nothing has to infer.

The evidence is from live runs on both harnesses, their events folded offline by a pure function. On the claude-sdk harness, eight runs gave the opener before the Helper started 8/8, the model ending its own turn after the spawn 8/8, a hand-back after the wake 5/5 with no tool or mechanism words, and Stop honoured within 0.06 s 3/3; they also showed the executor reporting an interrupted Helper as done 3/3 and the CLI's deferred tool search delaying first words in 5/8. On pi, the same fold and the same checks, unchanged, passed on its events with no pi branch; the adapter needed seven translations, all at the event boundary. A property suite of 338 cases against the first fold failed 228 of them across eleven bugs, among them a settled Round going back to running and a false Stop line, while the script's own 13 checks had passed; the fixed fold passes all 338. The recorded streams are the conformance fixtures under `engine/tests/superchat/rounds/`.

## Decision

**Principles.** Seven rules hold for everything below.

1. Words come from the harness and facts come from tool records. The engine writes no words, hides none and infers none. The one exception is the Stop line.
2. A Round is assigned at write time. Every item, Helper session and artifact carries its `round_id`.
3. The Round view is `fold(events)`, a pure function. Settlement, Stop, failure and events after settlement are cases of the fold, not rules beside it. The latest view is cached, and a reconnect reads it.
4. The strip shows the current tool with a live line digested from its streamed input, the elapsed time, the trail, one row per Helper with its live line and folder, the files and the checks. It never shows a fraction. A checklist sits beside it when the model wrote one. Expanding shows the full trail.
5. A turn ends when the model ends it. No hook ends a turn after `start_helper` or `suggest_follow_ups`. Chips come last.
6. Adapters translate and the engine stays neutral. The Claude adapter reads `content_block_start`, `input_json_delta`. The pi adapter reads `toolcall_start` and `toolcall_delta`, maps an abort to an `ExecutorError`, separates text blocks, unwraps the result envelope, and passes `call_id`. Both emit the same events.
7. The Assistant looks things up by judgement in memory, the journal, `recall` and `context_brief`. Nothing is pushed into a turn beyond the standing memory block.

**The Round is the unit of work.** A Round is one message from the person through its settlement. A steer joins the running turn's Round. A wake, notice or timer turn carries the Round of the Helper that caused it. Scheduled and system runs get a hidden Round of their own. A Round is settled when no turn runs, no Helper runs and no result is unconsumed, with a timeout when a wake post fails. "Ask" keeps its meaning: what the Assistant waits on the person for.

**One record, one fold.** Round events are stored with the Round, the Conversation, a sequence number, a kind, an actor and a body, written at dispatch, at turn end, at a Helper's terminal state, at a wake, at an artifact save, at chips, at a checklist change and at Stop. The web reads the folded view and its updates on the family stream. Activity rows group by `round_id`. Files attach to the Round that saved them, read from the record, not from a turn guessed by title.

**Stop stops the whole Round.** Stop ends the Round's turn, all its Helpers and its pending wakes, and leaves other Rounds alone. It records one `stop.requested` event and writes one line in the history, which the Assistant reads on its next turn so it can say honestly what finished. The cancellation reaches the parent, whatever status the executor reports.

**Helpers are Omnigent sessions on both harnesses.** The Assistant starts them only with `start_helper`. Harness-native sub-agents stay off on both harnesses. `start_helper` keeps `project` and `path`; with neither, the Helper inherits the Assistant's folder, and the folder is stored before the harness process starts on either executor. The folder shows as a fact on the Helper's row. Each Helper has one row whose live line comes from its tool input stream, and the Assistant's own work shows the same way.

**Helpers get context, not orders.** A Helper's brief is the Assistant's words plus facts the engine appends: the folder, the files in play and the preference lines that apply. Worker Helpers get `recall` and `recall_get`. They do not get `context_brief` or a dump of the whole profile.

**Context is pulled.** The eager brief build and its planner model call at message accept go, and so does the order to call `context_brief` first. `context_brief` builds when it is called. The system prompt describes what memory, the journal, `recall` and `context_brief` hold and when each is worth a look, and the Assistant decides. No new block is pushed into the turn.

**Each settled Round writes one journal entry.** On settlement the engine writes the ask, the outcome, the files, the Helpers, the folder and the duration to `memory/journal/<date>.md`, replacing the bare `Helper "<title>" <outcome>` line. Daily notes are untouched. A later `recall` on a repeated request finds the earlier Round.

**The checklist is the model's own words.** On both harnesses the Omnigent `checklist` tool takes a subject, active form and status, and folds to one `checklist.*` event. Claude's native task tools stay disallowed, so the model has one checklist tool, not two (amended 2026-10-19: the capability is Omnigent's, not the CLI's). A checklist is never bound to a Helper and drives nothing: no state, no refusal, no tick. ADR 0028 rejected a model-ticked plan because a ticked step claims work happened. A checklist claims nothing; the facts beside it come from the record, so the two cannot be confused.

**Delegation is judgement.** One deliverable that fits in one turn is done directly. Work in another folder, long work and parallel work go to Helpers. This is a line in the prompt. The engine adds no rule and refuses nothing for it: the step limit of [ADR 0007](0007-the-muse-delegates-multi-step-work.md) and its refusal are deleted.

**The plan machinery goes, with no compatibility layer.** The `steps` tool, the ledger, the refusal while no plan exists, the plan text in briefs, the binding of Helpers to steps and the web's merge by plan id are deleted. The check line stays, recomputed from the Helper's own calls.

**The harness and the model are settings.** Both come from the shared connection settings and nothing names them in code. Today's Claude configuration stays the default; pi becomes the recommended default once a run in the app matches the scripted runs.

**One vocabulary.** The agent is the **Assistant** everywhere; Muse, "Super Chat" and Nova are retired words, and the product is Superchat. The stored label value `superside-chat` stays an identifier. Legacy Conversations render from their transcript as today, without a strip, and are not backfilled.

## Consequences

- Engine: a `round_events` table, `round_id` at dispatch and on Helper labels, a pure fold with the property suite, a Round read route and a `round.updated` family event, Stop per Round, files by Round, the journal entry, Helper facts and folder, no eager brief.
- Executors: tool call start and input chunk events on both harnesses, a cancelled turn reported as cancelled, a step closed on `ToolCallComplete` by `call_id`, the checklist on both.
- Prompt: the delegation line, a short section on what memory, the journal, `recall` and `context_brief` hold, and no forced `context_brief` call.
- Web: one Round view drives the strip, the clock, Helper rows, files, the checklist and Stop.
- Deleted: about 2,300 engine lines and 950 web lines of plan machinery, the engine hand-back, `NOTE_WORDS`, the closed-after-card rule and the nudge reopen.
- Rules that stay rules: wake batching per Round, last text per turn, and the tool-to-words map and input digest kept as tool metadata.
- Kept from ADR 0028: the check line, background launch of Helpers, the de-duplication lock, one wake per run and the tool `effect` field.
- Delivery: a train of small PRs, each shipping alone.

## Reversed

From ADR 0028's decision:

- "**The model says where it is. The engine says what happened.** One tool, `plan`, … `add` steps …, `start` the step the model is now on, `reword` or `drop` a step by id. There is no way to mark a step done."
- "**Every call is credited to the active step.** … the Helper's calls go to the step the Helper was started for (`start_helper` names the step ids it takes)."
- "**One rule for every step.** … **nothing ran** when no call was; … The hand-back must name a step that reads nothing ran."
- "**The plan is the engine's view.** The web renders the engine's projection: steps with `state`, the active step's `active` form with the latest credited call, the fold of earlier steps, the summary line."
- "**The plan is always made and always followed.** Every request that takes more than one tool call gets a plan before the work …"
- "**Progress shows while it runs, then folds.** … (`5 of 5 done · 1:52 · Plan · Show details`, and `1 didn't run` …)".

From ADR 0028's fixes:

- Fix 1: "Everything that writes, runs or builds (`deck_*`, `artifact_save`, `live_update_*`, `sys_os_*`, `vault_fill`) is a Helper's". Delegation is judgement.
- Fix 2: "a wake turn that ends with no text is re-prompted once by the engine, and if it still says nothing the engine writes the one-line outcome from the record."
- Fix 15: "`start_helper` is refused while the turn has no plan, with an error that says to add the plan first".
- Fix 26: "When a plan's steps make or change files, the engine adds the closing check step".
- Fix 29: "A turn that owes the person a hand-back … cannot end on a closing card such as follow-up suggestions before it has said anything; the card waits for the words." (Amended 2026-10-19: back as a bounded `say_first` refusal, at most twice per turn; see Amendments.)
- Fix 30: "When the engine writes the hand-back for a silent wake, the transcript shows it in the run". The engine writes no hand-back.

From before ADR 0028:

- The transcript's hiding rule: "A short text (under `NOTE_WORDS` words) that more work follows is a working note even while it is the turn's last text so far … it is not a message either", and the rule that text after an ending card "is dropped instead of replacing the answer".
- `suggest_follow_ups` in `ENDS_TURN_TOOLS`, and `start_helper` added to it on `wave4`.
- ADR 0027's turn order: "Its first tool call is `context_brief` … built by code plus one Light planner call, started at accept", and the prompt's "For any request that uses tools, call `context_brief` first."

## Amendments

**Amended 2026-10-19 (fix wave).** What is true now, each with where it lives; the decision above stands where these do not touch it.

- **Settlement is never left to chance.** At start the Sweep reloads every Round with an event in the lookback, settled or not (`rounds/store.py` `rounds_since`, which replaced `open_turn_rounds`), so a Round left unsettled across a restart is not forgotten. A turn that fails before it reaches a runner ends its Round at once, failed with the failure's message (`rounds/record.py` `turn_failed`, called through `ON_TURN_FAILED` from `_record_turn_failure` in `server/routes/_sessions/helpers.py`).
- **A steered result is read when the session goes idle.** A wake that arrives while a turn runs is recorded `steered`; when the Conversation's session goes idle with nothing queued, a `session.idle` event counts those results read and the Round settles. The fold exposes `turn_due` (a turn is owed and has not started) and `read_on_idle` (`rounds/fold.py`).
- **A notice never moves the Round while a turn runs.** The session's Round label moves to a notice's Round only when no turn is running, so steers, Stop and file credit stay on the running Round (`rounds/record.py` `assign_round`).
- **Lost events end the Round.** A batch the store refuses is retried once after a second, then logged at ERROR; a Round whose Assistant `turn.ended` was in it ends failed with "round events not stored". `Recorder.idle` waits for events only, never for the journal; `Recorder.drain` (shutdown) waits for both.
- **Delivered is not settled.** The fold records `delivered_at` when the Assistant's turn showed its answer (a terminal card, the chips or a question) and no Helper of the Round runs or waits to be read; the strip reads settled from then with its clock stopped. Delivery tools never become the live tool (`now`). The Round itself still settles at its turn's end.
- **A card carries its hand-back sentence.** `render_card` takes `message` (one or two sentences, at most 400 characters) on terminal cards; the transcript draws it above the card, and the Round records it as the turn's words, both only when the turn wrote no text of its own (`cards/tools.py`, `transcript/blocks.py`, `rounds/record.py`). The words are still the model's, so principle 1 holds.
- **Fix 29 is back as a bounded refusal.** A terminal card without `message`, or chips, in a turn that has written no words gets a `say_first` result asking for the sentence, at most twice per turn, then is taken as it is (`cards/handlers.py`). Live wake turns on pi wrote no text whatever the prompt said; the tool argument is what the model reliably fills. The runner tells the handler whether the live turn has streamed text (`/mcp/execute`, `turn_said`), because card tools run there, not on the stream's dispatch path. The wake notice asks for the sentences before the card.
- **Adapters (principle 6).** pi: a turn ends at `agent_settled`, after pi's own retries, recovery and queued steers; a retry's `agent_start` clears the earlier error, and the `willRetry` special case and the 10 s post-error drain are deleted (`harnesses/pi_executor.py`). Claude: a `PreToolUse` hook notes each Omnigent MCP call's `tool_use_id`, so a call and its output are stored once under the model's own id, as on pi (`harnesses/claude_sdk_executor.py`).
- **The harness and the model are named to the model.** A per-turn prefix line (the Setup line, sent once to each warm harness session, never in the instructions so a model change keeps a warm client) names the model and the harness, and a turn after a change of either gets one prefix line saying what changed (`runner/app.py`, `prompt_prefix.setup_changed_line`). A harness switch binds the built-in agent itself, as a new Conversation does; the per-session copies a switch used to make are gone, and startup rebinds old ones (`conversation_store.rebind_switch_clones`).
- **Open task lines.** The asking Round is listed too while a Helper of it runs, with what it is doing now (`now`) and its finished `steps` (`tasks/service.py`, `tasks/record.py` `round_line`), so a status question asked mid-job is answered from the record.
- **Helper folder.** `start_helper` makes a missing folder inside the workspace instead of refusing it (`helpers/handlers.py`).
- **Activity.** A studio turn's file carries the Round that created it (`round_id` of its first version), so the panel shows one row per request with its Helper, its file and the file's later edits under it (`activity/derive.py`, `web/app/src/features/activity/activityTree.ts`).

**Amended 2026-10-25: Helpers started together wake their parent once.** "Work through all" started three Helpers in one turn, and each Result woke the Assistant on its own: three wake turns of interim chatter, one steered into another.

- Helpers dispatched in one parent turn are a group, named by that turn's response id and stamped on each child (`omnigent.subagent.group`). A Result whose sibling still runs waits in the inbox; the last to settle (completed, failed, cancelled, or failed by the existing launch reaper) wakes the parent once with every Result listed (`runner/app.py` `subagent_group_waits`, `_schedule_subagent_wake`). A lone Helper wakes as before; a person's message still gets its turn at once.
- Restart recovery reads the group and the inherited mode from the child's labels and wakes only after its scan, so a recovered Result still waits for a sibling that runs.
- The prompt asks for one short line after the last `start_helper`, and for the final reply's items as a `- ✓` list.
- Display: the pi adapter sets a new text block apart with a blank line, as the Claude adapter does, so text after a steer no longer glues to the text before it ("one.The"); the web reads the transcript again whenever streamed text is stored, so the streamed copy never stays under the stored one.
- **No words before the jobs (2026-10-25, after the train run).** One "Work through all" still showed two lines: "I'm starting the Morocco items now…" before the `start_helper` calls and "I've started four jobs…" after them. The prompt's first rule (speak before any tool call, from [0022](0022-the-orchestrator-narrates-while-helpers-work.md) and [0026](0026-a-turn-speaks-in-a-second.md)) asked for the first, the work-through rule for the second. A work-through request is now the one exception to the first rule: its jobs start with no words before them and one short line follows the last. The second-to-first-sight budget still holds: the started jobs are the first visible event (the Round's strip and the work card).
